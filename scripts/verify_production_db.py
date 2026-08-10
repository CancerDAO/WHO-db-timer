"""Validate a built database and print a machine-readable deployment summary."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import sqlite3
from pathlib import Path

REQUIRED_TABLES = {
    "trial_master",
    "trial_registry_ids",
    "trial_interventions",
    "trial_eligibility_criteria",
    "trial_country_records",
    "trial_sites",
    "trial_search_fts",
    "database_metadata",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--minimum-trials", type=int, default=1000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = args.db.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"database does not exist: {path}")

    with closing(sqlite3.connect(path)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = len(list(conn.execute("PRAGMA foreign_key_check")))
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        }
        missing = sorted(REQUIRED_TABLES - tables)
        trial_count = int(conn.execute("SELECT COUNT(*) FROM trial_master").fetchone()[0]) if not missing else 0
        fts_count = int(conn.execute("SELECT COUNT(*) FROM trial_search_fts").fetchone()[0]) if not missing else 0
        metadata = (
            {str(key): str(value) for key, value in conn.execute("SELECT key,value FROM database_metadata")}
            if not missing else {}
        )

    checks = {
        "integrity_ok": integrity.casefold() == "ok",
        "foreign_keys_ok": foreign_key_errors == 0,
        "required_tables_ok": not missing,
        "minimum_trials_ok": trial_count >= args.minimum_trials,
        "fts_coverage_ok": trial_count > 0 and fts_count == trial_count,
        "metadata_ok": bool(metadata),
    }
    result = {
        "database": str(path),
        "bytes": path.stat().st_size,
        "trial_count": trial_count,
        "fts_count": fts_count,
        "missing_tables": missing,
        "foreign_key_errors": foreign_key_errors,
        "database_as_of": metadata.get("source_searched_through") or metadata.get("database_built_at"),
        "checks": checks,
        "passed": all(checks.values()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
