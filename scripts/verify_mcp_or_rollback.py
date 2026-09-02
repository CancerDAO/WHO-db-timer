"""Verify the promoted MCP endpoint and atomically restore its backup on failure."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-db", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path("/etc/who-mcp.env"))
    parser.add_argument("--service", default="who-mcp.service")
    parser.add_argument("--attempts", type=int, default=20)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    return parser.parse_args()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    return values


def write_status(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def decode_message(body: str) -> dict[str, Any]:
    stripped = body.strip()
    if stripped.startswith("{"):
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    for event in stripped.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
        )
        if data:
            value = json.loads(data)
            if isinstance(value, dict):
                return value
    raise RuntimeError("MCP endpoint returned no JSON-RPC message")


def initialize_mcp(env: dict[str, str], timeout: float = 5.0) -> dict[str, Any]:
    host = env.get("WHO_MCP_HOST", "127.0.0.1")
    port = int(env.get("WHO_MCP_PORT", "8000"))
    key = env.get("WHO_MCP_API_KEY", "")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("post-promotion verification only permits a loopback MCP host")
    if not key:
        raise RuntimeError("WHO_MCP_API_KEY is missing")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "who-db-refresh-healthcheck", "version": "1.0"},
        },
    }
    request = urllib.request.Request(
        f"http://{host}:{port}/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2024-11-05",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"MCP initialize returned HTTP {response.status}")
        message = decode_message(response.read().decode("utf-8", errors="replace"))
    result = message.get("result")
    if not isinstance(result, dict) or not result.get("protocolVersion"):
        raise RuntimeError("MCP initialize response is missing protocolVersion")
    return {
        "protocol_version": result["protocolVersion"],
        "server_info": result.get("serverInfo") or {},
    }


def wait_for_mcp(env: dict[str, str], attempts: int, delay: float) -> dict[str, Any]:
    last_error = "not attempted"
    for _ in range(max(1, attempts)):
        try:
            return initialize_mcp(env)
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(max(0.0, delay))
    raise RuntimeError(last_error)


def verify_query_layer(production: Path) -> dict[str, Any]:
    service_root = production.resolve().parent.parent / "mcp_service"
    sys.path.insert(0, str(service_root))
    try:
        from query_service import (  # type: ignore[import-not-found]
            database_metadata,
            search_trials_multidimensional,
        )

        metadata = database_metadata(production)
        search = search_trials_multidimensional(
            production,
            general_terms=["cancer"],
            recruitment_statuses=["recruiting"],
            limit=1,
        )
    finally:
        if sys.path and sys.path[0] == str(service_root):
            sys.path.pop(0)
    results = search.get("results") if isinstance(search, dict) else None
    if not metadata.get("database_as_of"):
        raise RuntimeError("database_metadata returned no database_as_of watermark")
    if not isinstance(results, list) or not results:
        raise RuntimeError("production query smoke test returned no recruiting cancer trial")
    return {
        "database_as_of": metadata["database_as_of"],
        "search_result_count": len(results),
    }


def restore_backup(production: Path, backup: Path) -> None:
    if not backup.is_file():
        raise RuntimeError(f"rollback backup is missing: {backup}")
    temporary = production.with_suffix(production.suffix + ".rollback")
    shutil.copy2(backup, temporary)
    backup_stat = backup.stat()
    if os.name != "nt":
        os.chown(temporary, backup_stat.st_uid, backup_stat.st_gid)
    os.chmod(temporary, backup_stat.st_mode)
    os.replace(temporary, production)


def systemctl(action: str, service: str, *, check: bool = True) -> None:
    subprocess.run(["/usr/bin/systemctl", action, service], check=check)


def main() -> None:
    args = parse_args()
    status = json.loads(args.status_file.read_text(encoding="utf-8"))
    if status.get("status") != "promoted":
        raise SystemExit("refresh status is not promoted; refusing post-promotion check")
    env = read_env(args.env_file)
    try:
        smoke = wait_for_mcp(env, args.attempts, args.delay_seconds)
        smoke["query_layer"] = verify_query_layer(args.production_db)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        backup = Path(str(status.get("backup") or ""))
        systemctl("stop", args.service, check=False)
        try:
            restore_backup(args.production_db, backup)
            systemctl("start", args.service)
            rollback_smoke = wait_for_mcp(env, args.attempts, args.delay_seconds)
            rollback_smoke["query_layer"] = verify_query_layer(args.production_db)
            status.update({
                "status": "rolled_back", "post_promotion_error": failure,
                "rollback_verified": True, "rollback_smoke": rollback_smoke,
            })
        except Exception as rollback_exc:
            status.update({
                "status": "rollback_failed", "post_promotion_error": failure,
                "rollback_verified": False,
                "rollback_error": f"{type(rollback_exc).__name__}: {rollback_exc}",
            })
        write_status(args.status_file, status)
        raise SystemExit(1)
    status["post_promotion_smoke"] = {"passed": True, **smoke}
    write_status(args.status_file, status)


if __name__ == "__main__":
    main()
