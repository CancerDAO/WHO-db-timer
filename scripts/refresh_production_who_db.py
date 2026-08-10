"""Build a fresh WHO database in staging and atomically promote it after validation."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - deployment target is Linux
    fcntl = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "who_ictrp_cancer_trials.db"
REQUIRED_TABLES = {
    "trial_master", "trial_registry_ids", "trial_interventions",
    "trial_eligibility_criteria", "trial_country_records", "trial_sites",
    "trial_search_fts", "database_metadata",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--database-mode", choices=("auto", "who-only", "hybrid"), default="auto")
    parser.add_argument(
        "--ctgov-db", type=Path,
        default=PROJECT_ROOT.parent / "global-cancer-trials-db" / "sources" / "clinicaltrials_gov" / "data" / "ctgov_cancer_trials.db",
    )
    parser.add_argument(
        "--ctis-db", type=Path,
        default=PROJECT_ROOT.parent / "global-cancer-trials-db" / "sources" / "eu_ctis" / "data" / "ctis_cancer_trials.db",
    )
    parser.add_argument("--strategy", type=Path, default=PROJECT_ROOT / "config" / "who_search_strategy.yaml")
    parser.add_argument("--terms", type=Path, default=PROJECT_ROOT / "config" / "cancer_recall_terms.yaml")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--xvfb-run", default="xvfb-run")
    parser.add_argument("--headful", action="store_true", default=True)
    parser.add_argument("--no-headful", dest="headful", action="store_false")
    parser.add_argument("--include-optional", action="store_true")
    parser.add_argument(
        "--use-existing-staging", action="store_true",
        help="Validate and promote an already completed .next.db after a publication-step failure.",
    )
    parser.add_argument("--minimum-trials", type=int, default=1000)
    parser.add_argument("--minimum-old-ratio", type=float, default=0.70)
    parser.add_argument("--backup-count", type=int, default=3)
    parser.add_argument("--lock-file", type=Path, default=PROJECT_ROOT / "data" / ".who-refresh.lock")
    parser.add_argument("--status-file", type=Path, default=PROJECT_ROOT / "data" / "reports" / "scheduled_refresh_status.json")
    return parser.parse_args()


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def trial_count(path: Path) -> int:
    if not path.is_file():
        return 0
    conn = sqlite3.connect(path)
    try:
        if "trial_master" not in table_names(conn):
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM trial_master").fetchone()[0])
    finally:
        conn.close()


def production_database_mode(path: Path) -> str:
    """Detect whether the current production artifact contains merged source provenance."""
    if not path.is_file():
        return "who-only"
    conn = sqlite3.connect(path)
    try:
        tables = table_names(conn)
        if "trial_source_provenance" in tables:
            sources = {
                str(row[0]).strip().casefold()
                for row in conn.execute("SELECT DISTINCT source_name FROM trial_source_provenance")
            }
            if sources - {"", "who", "who_ictrp"}:
                return "hybrid"
    finally:
        conn.close()
    return "who-only"


def validate_database(path: Path, *, old_count: int, minimum_trials: int, minimum_old_ratio: float) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("staging database is missing or empty")
    conn = sqlite3.connect(path)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.casefold() != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        foreign_keys = list(conn.execute("PRAGMA foreign_key_check"))
        if foreign_keys:
            raise RuntimeError(f"SQLite foreign_key_check returned {len(foreign_keys)} row(s)")
        tables = table_names(conn)
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError(f"staging database is missing required tables: {missing}")
        count = int(conn.execute("SELECT COUNT(*) FROM trial_master").fetchone()[0])
        if count < minimum_trials:
            raise RuntimeError(f"staging trial count {count} is below minimum {minimum_trials}")
        if old_count and count < int(old_count * minimum_old_ratio):
            raise RuntimeError(
                f"staging trial count {count} is below {minimum_old_ratio:.0%} of production count {old_count}"
            )
        metadata = conn.execute("SELECT COUNT(*) FROM database_metadata").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM trial_search_fts").fetchone()[0]
        if not metadata or not fts:
            raise RuntimeError("matching metadata or FTS index is empty")
    finally:
        conn.close()
    return {
        "database": str(path), "bytes": path.stat().st_size,
        "trial_count": count, "fts_count": int(fts), "integrity_check": integrity,
    }


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def rotate_backups(production: Path, count: int) -> Path | None:
    if not production.is_file() or count < 1:
        return None
    backup_dir = production.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{production.stem}-{stamp}{production.suffix}"
    shutil.copy2(production, backup)
    backups = sorted(backup_dir.glob(f"{production.stem}-*{production.suffix}"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in backups[count:]:
        stale.unlink()
    return backup


def build_command(args: argparse.Namespace, staging: Path) -> list[str]:
    command = [
        args.python, str(PROJECT_ROOT / "scripts" / "run_full_who_pipeline.py"),
        "--reset", "--strategy", str(args.strategy), "--terms", str(args.terms),
        "--out", str(staging), "--download-timeout-ms", "1800000",
        "--download-retries", "3",
    ]
    if args.include_optional:
        command.append("--include-optional")
    if args.headful:
        command.append("--headful")
        command = [args.xvfb_run, "-a", *command]
    return command


def main() -> None:
    args = parse_args()
    production = args.production_db.expanduser().resolve()
    production.parent.mkdir(parents=True, exist_ok=True)
    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    status: dict[str, Any] = {"status": "running", "started_at": started, "production_db": str(production)}
    write_status(args.status_file, status)
    staging = production.with_name(production.stem + ".next" + production.suffix)
    who_staging = production.with_name(production.stem + ".who-next" + production.suffix)
    with args.lock_file.open("a+", encoding="utf-8") as lock:
        if fcntl is None:
            raise SystemExit("scheduled refresh requires Linux fcntl locking")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("another WHO database refresh is already running") from exc
        try:
            old_count = trial_count(production)
            mode = production_database_mode(production) if args.database_mode == "auto" else args.database_mode
            status.update({
                "old_trial_count": old_count, "database_mode": mode,
                "used_existing_staging": bool(args.use_existing_staging),
            })
            if args.use_existing_staging:
                if not staging.is_file():
                    raise RuntimeError(f"existing staging database does not exist: {staging}")
                if mode == "hybrid" and production_database_mode(staging) != "hybrid":
                    raise RuntimeError("refusing to promote a WHO-only staging database over a hybrid production database")
            else:
                for base in (staging, who_staging):
                    for suffix in ("", "-wal", "-shm"):
                        candidate = Path(str(base) + suffix)
                        if candidate.exists():
                            candidate.unlink()
                build_target = who_staging if mode == "hybrid" else staging
                command = build_command(args, build_target)
                status["command"] = command
                write_status(args.status_file, status)
                subprocess.run(command, cwd=PROJECT_ROOT, check=True)
            if mode == "hybrid" and not args.use_existing_staging:
                missing_sources = [
                    str(path) for path in (args.ctgov_db, args.ctis_db) if not path.is_file()
                ]
                if missing_sources:
                    raise RuntimeError(
                        "production database is hybrid but required source databases are missing: "
                        + ", ".join(missing_sources)
                    )
                hybrid_command = [
                    args.python, str(PROJECT_ROOT / "scripts" / "build_hybrid_matching_db.py"),
                    "--who-db", str(who_staging), "--ctgov-db", str(args.ctgov_db),
                    "--ctis-db", str(args.ctis_db), "--out", str(staging),
                    "--strategy", str(args.strategy),
                ]
                status["hybrid_command"] = hybrid_command
                write_status(args.status_file, status)
                subprocess.run(hybrid_command, cwd=PROJECT_ROOT, check=True)
            write_status(args.status_file, status)
            validation = validate_database(
                staging, old_count=old_count, minimum_trials=args.minimum_trials,
                minimum_old_ratio=args.minimum_old_ratio,
            )
            backup = rotate_backups(production, args.backup_count)
            os.replace(staging, production)
            who_staging.unlink(missing_ok=True)
            status.update({
                "status": "promoted", "finished_at": utc_now(), "validation": validation,
                "backup": str(backup) if backup else None,
            })
            write_status(args.status_file, status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
        except Exception as exc:
            status.update({"status": "failed", "finished_at": utc_now(), "error": f"{type(exc).__name__}: {exc}"})
            write_status(args.status_file, status)
            raise


if __name__ == "__main__":
    main()
