from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from who_common import DEFAULT_DB, utc_now


SCHEMA_VERSION = "3"
SEARCH_INDEX_VERSION = "fts5_multidimensional_v1"
COUNTED_TABLES = (
    "trial_master",
    "trial_registry_ids",
    "trial_interventions",
    "trial_eligibility_criteria",
    "trial_sites",
    "trial_country_records",
    "trial_source_provenance",
    "who_search_runs",
    "who_trial_records",
    "who_recall_hits",
)


def ensure_matching_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS database_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS database_table_stats (
            table_name TEXT PRIMARY KEY,
            row_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS trial_search_fts USING fts5(
            trial_uid UNINDEXED, title, scientific_title, brief_summary,
            disease_text, intervention_text, eligibility_text,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE INDEX IF NOT EXISTS idx_trial_master_primary_id_nocase
            ON trial_master(primary_registry_id COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_trial_master_status_registration
            ON trial_master(recruitment_status_normalized, registration_date);
        CREATE INDEX IF NOT EXISTS idx_trial_master_last_fetched
            ON trial_master(last_fetched_at);
        CREATE INDEX IF NOT EXISTS idx_trial_registry_ids_id_nocase
            ON trial_registry_ids(registry_id COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_trial_sites_country_trial
            ON trial_sites(country COLLATE NOCASE, trial_uid);
        CREATE INDEX IF NOT EXISTS idx_trial_country_records_country_trial
            ON trial_country_records(country COLLATE NOCASE, trial_uid);
        CREATE INDEX IF NOT EXISTS idx_who_search_runs_searched_at
            ON who_search_runs(searched_at);
        """
    )


def _set_metadata(conn: sqlite3.Connection, key: str, value: Any, updated_at: str) -> None:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """INSERT INTO database_metadata(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, updated_at),
    )


def _strategy_digest(strategy_path: Path | None) -> str:
    if not strategy_path or not strategy_path.exists():
        return ""
    return hashlib.sha256(strategy_path.read_bytes()).hexdigest()


def rebuild_search_index(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM trial_search_fts")
    conn.execute(
        """
        INSERT INTO trial_search_fts(
            trial_uid, title, scientific_title, brief_summary, disease_text,
            intervention_text, eligibility_text
        )
        WITH interventions AS (
            SELECT trial_uid, group_concat(COALESCE(intervention_name_raw, ''), ' | ') AS text
            FROM trial_interventions GROUP BY trial_uid
        ), criteria AS (
            SELECT trial_uid, group_concat(COALESCE(criterion_text, ''), ' | ') AS text
            FROM trial_eligibility_criteria GROUP BY trial_uid
        )
        SELECT tm.trial_uid, COALESCE(tm.title, ''), COALESCE(tm.scientific_title, ''),
               COALESCE(tm.brief_summary, ''), COALESCE(tm.disease_text, ''),
               trim(COALESCE(tm.intervention_summary, '') || ' | ' || COALESCE(i.text, '')),
               COALESCE(c.text, '')
        FROM trial_master tm
        LEFT JOIN interventions i ON i.trial_uid = tm.trial_uid
        LEFT JOIN criteria c ON c.trial_uid = tm.trial_uid
        """
    )
    return int(conn.execute("SELECT COUNT(*) FROM trial_search_fts").fetchone()[0])


def refresh_cached_statistics(conn: sqlite3.Connection, updated_at: str) -> dict[str, int]:
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts: dict[str, int] = {}
    for table in COUNTED_TABLES:
        if table not in existing:
            continue
        count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        counts[table] = count
        conn.execute(
            """INSERT INTO database_table_stats(table_name, row_count, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(table_name) DO UPDATE SET row_count=excluded.row_count, updated_at=excluded.updated_at""",
            (table, count, updated_at),
        )
    classifiable = int(conn.execute("""
        SELECT COUNT(DISTINCT trial_uid) FROM (
            SELECT trial_uid FROM trial_sites WHERE trim(COALESCE(country, '')) <> ''
            UNION ALL
            SELECT trial_uid FROM trial_country_records WHERE trim(COALESCE(country, '')) <> ''
        )
    """).fetchone()[0])
    _set_metadata(conn, "country_classifiable_trials", classifiable, updated_at)
    return counts


def optimize_database(
    conn: sqlite3.Connection,
    *,
    built_at: str | None = None,
    strategy_path: Path | None = None,
    coverage_scope: dict[str, Any] | None = None,
    rebuild_fts: bool = True,
) -> dict[str, Any]:
    now = built_at or utc_now()
    ensure_matching_schema(conn)
    fts_rows = rebuild_search_index(conn) if rebuild_fts else int(
        conn.execute("SELECT COUNT(*) FROM trial_search_fts").fetchone()[0]
    )
    counts = refresh_cached_statistics(conn, now)
    source_searched_through = conn.execute("SELECT MAX(searched_at) FROM who_search_runs").fetchone()[0] or ""
    max_fetched_at = conn.execute("SELECT MAX(last_fetched_at) FROM trial_master").fetchone()[0] or ""
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "search_index_version": SEARCH_INDEX_VERSION,
        "database_built_at": now,
        "source_searched_through": source_searched_through,
        "max_fetched_at": max_fetched_at,
        "strategy_path": str(strategy_path) if strategy_path else "",
        "strategy_sha256": _strategy_digest(strategy_path),
        "coverage_scope": coverage_scope or {"source": "WHO ICTRP"},
        "fts_row_count": fts_rows,
        "stats_updated_at": now,
    }
    for key, value in metadata.items():
        _set_metadata(conn, key, value, now)
    conn.execute("ANALYZE")
    conn.commit()
    return {"metadata": metadata, "table_counts": counts}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or refresh WHO trial matching indexes and metadata.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--strategy", type=Path)
    parser.add_argument("--skip-fts", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = sqlite3.connect(args.db)
    try:
        result = optimize_database(
            conn,
            strategy_path=args.strategy,
            coverage_scope={"source": "WHO ICTRP", "recruitment_status": ["recruiting"]},
            rebuild_fts=not args.skip_fts,
        )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

