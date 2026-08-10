"""Dependency-free MCP stdio server compatible with Python 3.9.

Implements the MCP JSON-RPC lifecycle and tools used by this project. The query
implementation remains in query_service/plan_search and is read-only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from plan_search import execute_search_plan as query_search_plan
from query_service import (
    country_stats as query_country_stats,
    database_metadata as query_database_metadata,
    database_summary as query_database_summary,
    get_trial as query_get_trial,
    search_trials_multidimensional as query_multidimensional,
)

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "who_ictrp_cancer_trials.db"
DB_PATH = Path(os.environ.get("WHO_ICTRP_DB", DEFAULT_DB)).resolve()
PROTOCOL_VERSION = "2024-11-05"


def database_metadata() -> Any:
    return query_database_metadata(DB_PATH)


def database_summary() -> Any:
    return query_database_summary(DB_PATH)


def execute_search_plan(search_plan: dict[str, Any], country: str = "", max_per_query: int = 500, total_limit: int = 5000) -> Any:
    return query_search_plan(DB_PATH, search_plan, country=country, max_per_query=max_per_query, total_limit=total_limit)


def get_trial(registry_id: str) -> Any:
    return query_get_trial(DB_PATH, registry_id)


def search_trials_multidimensional(**kwargs: Any) -> Any:
    return query_multidimensional(DB_PATH, **kwargs)


def country_stats(limit: int = 50) -> Any:
    return query_country_stats(DB_PATH, limit)


TOOLS: dict[str, tuple[Callable[..., Any], dict[str, Any], str]] = {
    "database_metadata": (database_metadata, {"type": "object", "properties": {}, "additionalProperties": False}, "Return database build and WHO watermark metadata."),
    "database_summary": (database_summary, {"type": "object", "properties": {}, "additionalProperties": False}, "Return cached database counts and coverage."),
    "execute_search_plan": (execute_search_plan, {
        "type": "object", "required": ["search_plan"],
        "properties": {
            "search_plan": {"type": "object"}, "country": {"type": "string", "default": ""},
            "max_per_query": {"type": "integer", "default": 500},
            "total_limit": {"type": "integer", "default": 5000},
        }, "additionalProperties": False,
    }, "Execute the complete multidimensional clinical matching search plan."),
    "get_trial": (get_trial, {
        "type": "object", "required": ["registry_id"],
        "properties": {"registry_id": {"type": "string"}}, "additionalProperties": False,
    }, "Fetch canonical trial detail including eligibility and site evidence."),
    "search_trials_multidimensional": (search_trials_multidimensional, {
        "type": "object", "properties": {
            "general_terms": {"type": ["array", "null"], "items": {"type": "string"}},
            "condition_terms": {"type": ["array", "null"], "items": {"type": "string"}},
            "biomarker_terms": {"type": ["array", "null"], "items": {"type": "string"}},
            "intervention_terms": {"type": ["array", "null"], "items": {"type": "string"}},
            "eligibility_terms": {"type": ["array", "null"], "items": {"type": "string"}},
            "country": {"type": "string"}, "recruitment_statuses": {"type": ["array", "null"], "items": {"type": "string"}},
            "interventional_only": {"type": "boolean"}, "limit": {"type": "integer"}, "offset": {"type": "integer"},
        }, "additionalProperties": False,
    }, "Search independent clinical dimensions with pagination."),
    "country_stats": (country_stats, {
        "type": "object", "properties": {"limit": {"type": "integer", "default": 50}}, "additionalProperties": False,
    }, "Return trial counts by country."),
}


def send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def success(request_id: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        success(request_id, {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "who-ictrp-cancer-trials", "version": "3.1-compat"},
        })
    elif method in {"notifications/initialized", "notifications/cancelled"}:
        return
    elif method == "ping":
        success(request_id, {})
    elif method == "tools/list":
        success(request_id, {"tools": [
            {"name": name, "description": description, "inputSchema": schema}
            for name, (_, schema, description) in TOOLS.items()
        ]})
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name not in TOOLS:
            error(request_id, -32601, f"Unknown tool: {name}")
            return
        try:
            value = TOOLS[name][0](**(params.get("arguments") or {}))
            success(request_id, {
                "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "structuredContent": value,
                "isError": False,
            })
        except Exception as exc:
            success(request_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
    elif request_id is not None:
        error(request_id, -32601, f"Unknown method: {method}")


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:
            error(None, -32700, f"Parse error: {exc}")


if __name__ == "__main__":
    main()