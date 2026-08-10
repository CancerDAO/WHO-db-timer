from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WHO_DB = PROJECT_ROOT / "data" / "who_ictrp_cancer_trials.db"
DEFAULT_EXISTING_ROOT = PROJECT_ROOT.parent / "global-cancer-trials-db"
SOURCE_DBS = [
    ("clinicaltrials_gov", "sources/clinicaltrials_gov/data/ctgov_cancer_trials.db"),
    ("china_chictr", "sources/china_chictr/data/chictr_cancer_trials.db"),
    ("eu_ctis", "sources/eu_ctis/data/ctis_cancer_trials.db"),
    ("japan_umin_ctr", "sources/japan_umin_ctr/data/umin_ctr_cancer_trials.db"),
]
SHARED_TABLES = [
    "raw_trial_records",
    "trial_master",
    "trial_registry_ids",
    "trial_interventions",
    "trial_eligibility_criteria",
    "trial_sites",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate WHO ICTRP DB quality, coverage, and country-routing reports.")
    parser.add_argument("--who-db", type=Path, default=DEFAULT_WHO_DB)
    parser.add_argument("--existing-root", type=Path, default=DEFAULT_EXISTING_ROOT)
    parser.add_argument("--out-json", type=Path, default=PROJECT_ROOT / "data" / "reports" / "who_quality_report.json")
    parser.add_argument("--out-md", type=Path, default=PROJECT_ROOT / "data" / "reports" / "who_quality_report.md")
    parser.add_argument("--sample-size", type=int, default=50)
    return parser.parse_args()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {qident(table)}").fetchone()[0]) if table_exists(conn, table) else 0


def collect_ids(conn: sqlite3.Connection) -> set[str]:
    ids: set[str] = set()
    if table_exists(conn, "trial_master"):
        for row in conn.execute("SELECT primary_registry_id FROM trial_master"):
            value = (row[0] or "").strip().upper()
            if value:
                ids.add(value)
    if table_exists(conn, "trial_registry_ids"):
        for row in conn.execute("SELECT registry_id FROM trial_registry_ids"):
            value = (row[0] or "").strip().upper()
            if value:
                ids.add(value)
    return ids


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def completeness(conn: sqlite3.Connection) -> dict[str, Any]:
    total = table_count(conn, "trial_master")
    fields = {
        "title": "title",
        "scientific_title": "scientific_title",
        "disease_text": "disease_text",
        "intervention_summary": "intervention_summary",
        "countries_text": "countries",
        "registration_date": "registration_date",
        "start_date": "start_date",
        "source_url": "source_url",
        "recruitment_status_raw": "recruitment_status_raw",
    }
    out: dict[str, Any] = {"total_trials": total, "fields": {}}
    if not total:
        return out
    for label, col in fields.items():
        n = scalar(conn, f"SELECT COUNT(*) FROM trial_master WHERE COALESCE({qident(col)}, '') <> ''")
        out["fields"][label] = {"nonblank": n, "pct": round(n * 100 / total, 2)}
    with_sites = scalar(conn, "SELECT COUNT(DISTINCT trial_uid) FROM trial_sites WHERE COALESCE(country, '') <> ''")
    out["trials_with_site_country"] = {"count": with_sites, "pct": round(with_sites * 100 / total, 2)}
    return out


def country_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = table_count(conn, "trial_master")
    rows = conn.execute(
        """
        SELECT country, COUNT(DISTINCT trial_uid) AS n
        FROM trial_sites
        WHERE COALESCE(country, '') <> ''
        GROUP BY country
        ORDER BY n DESC, country
        """
    ).fetchall() if table_exists(conn, "trial_sites") else []
    trials_with_country = scalar(conn, "SELECT COUNT(DISTINCT trial_uid) FROM trial_sites WHERE COALESCE(country, '') <> ''") if table_exists(conn, "trial_sites") else 0
    multi_country = scalar(
        conn,
        """
        SELECT COUNT(*) FROM (
            SELECT trial_uid, COUNT(DISTINCT country) AS n
            FROM trial_sites
            WHERE COALESCE(country, '') <> ''
            GROUP BY trial_uid
            HAVING n > 1
        )
        """,
    ) if table_exists(conn, "trial_sites") else 0
    return {
        "total_trials": total,
        "trials_with_country": trials_with_country,
        "trials_without_country": max(total - trials_with_country, 0),
        "country_classifiable_pct": round(trials_with_country * 100 / total, 2) if total else 0,
        "multi_country_trials": multi_country,
        "distinct_countries": len(rows),
        "top_countries": [{"country": row[0], "trials": row[1]} for row in rows[:50]],
    }


def source_coverage(who_ids: set[str], root: Path, sample_size: int) -> list[dict[str, Any]]:
    out = []
    for name, rel in SOURCE_DBS:
        path = root / rel
        item: dict[str, Any] = {"source": name, "db_path": str(path), "exists": path.exists()}
        if not path.exists():
            item["warning"] = "missing source DB"
            out.append(item)
            continue
        conn = sqlite3.connect(path)
        try:
            ids = collect_ids(conn)
        finally:
            conn.close()
        overlap = who_ids & ids
        missing = sorted(ids - who_ids)
        item.update(
            {
                "source_unique_registry_ids": len(ids),
                "overlap_ids": len(overlap),
                "missing_ids": len(missing),
                "coverage_pct": round(len(overlap) * 100 / len(ids), 2) if ids else 0,
                "missing_sample": missing[:sample_size],
            }
        )
        out.append(item)
    return out


def write_reports(payload: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# WHO ICTRP Quality Report", "", f"Generated at: `{payload['generated_at']}`", f"Database: `{payload['who_db']}`", ""]
    lines.extend(["## Integrity", ""])
    for key, value in payload["integrity"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Table Counts", ""])
    for key, value in payload["table_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Completeness", ""])
    for key, value in payload["completeness"].get("fields", {}).items():
        lines.append(f"- {key}: {value['nonblank']} ({value['pct']}%)")
    site_country = payload["completeness"].get("trials_with_site_country", {})
    lines.append(f"- trials_with_site_country: {site_country.get('count', 0)} ({site_country.get('pct', 0)}%)")
    lines.extend(["", "## Recall", ""])
    for row in payload["recall"].get("confidence_counts", []):
        lines.append(f"- {row['cancer_recall_confidence'] or 'blank'}: {row['n']}")
    lines.extend(["", "## Existing Source Coverage", "", "| Source | Existing IDs | Overlap | Missing | Coverage |", "| --- | ---: | ---: | ---: | ---: |"])
    for row in payload["existing_source_coverage"]:
        lines.append(f"| {row['source']} | {row.get('source_unique_registry_ids', 0)} | {row.get('overlap_ids', 0)} | {row.get('missing_ids', 0)} | {row.get('coverage_pct', 0)}% |")
    cs = payload["country_routing"]
    lines.extend(["", "## Country / Region Routing", ""])
    lines.append(f"- classifiable_trials: {cs['trials_with_country']} / {cs['total_trials']} ({cs['country_classifiable_pct']}%)")
    lines.append(f"- trials_without_country: {cs['trials_without_country']}")
    lines.append(f"- multi_country_trials: {cs['multi_country_trials']}")
    lines.append(f"- distinct_countries: {cs['distinct_countries']}")
    lines.extend(["", "Top countries:", ""])
    for row in cs["top_countries"][:25]:
        lines.append(f"- {row['country']}: {row['trials']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    if not args.who_db.exists():
        raise SystemExit(f"WHO DB missing: {args.who_db}")
    conn = sqlite3.connect(args.who_db)
    conn.row_factory = sqlite3.Row
    try:
        integrity = {
            "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
            "db_size_bytes": args.who_db.stat().st_size,
        }
        table_counts = {table: table_count(conn, table) for table in SHARED_TABLES}
        for table in ["who_search_runs", "who_download_hits", "who_trial_records", "who_recall_hits"]:
            table_counts[table] = table_count(conn, table)
        confidence_counts = [dict(row) for row in conn.execute(
            "SELECT cancer_recall_confidence, COUNT(*) AS n FROM trial_master GROUP BY cancer_recall_confidence ORDER BY n DESC"
        )] if table_exists(conn, "trial_master") else []
        recall_by_field = [dict(row) for row in conn.execute(
            "SELECT field_name, hit_type, COUNT(DISTINCT trial_id) AS trials FROM who_recall_hits GROUP BY field_name, hit_type ORDER BY trials DESC"
        )] if table_exists(conn, "who_recall_hits") else []
        register_counts = [dict(row) for row in conn.execute(
            "SELECT source_register, COUNT(*) AS n FROM who_trial_records GROUP BY source_register ORDER BY n DESC LIMIT 50"
        )] if table_exists(conn, "who_trial_records") else []
        who_ids = collect_ids(conn)
        payload = {
            "generated_at": utc_now(),
            "who_db": str(args.who_db),
            "integrity": integrity,
            "table_counts": table_counts,
            "completeness": completeness(conn),
            "recall": {"confidence_counts": confidence_counts, "recall_by_field": recall_by_field},
            "source_register_counts": register_counts,
            "existing_source_coverage": source_coverage(who_ids, args.existing_root, args.sample_size),
            "country_routing": country_stats(conn),
        }
    finally:
        conn.close()
    write_reports(payload, args.out_json, args.out_md)
    print(json.dumps({"trial_master": payload["table_counts"].get("trial_master", 0), "classifiable_pct": payload["country_routing"]["country_classifiable_pct"], "report": str(args.out_md)}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
