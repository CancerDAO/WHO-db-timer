from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from matching_db import optimize_database
from who_common import utc_now

ACTIVE_STATUSES = (
    "recruiting", "not_yet_recruiting", "not yet recruiting",
    "enrolling by invitation", "enrolling_by_invitation",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a hybrid oncology matching DB from WHO ICTRP plus official CT.gov and CTIS source DBs."
    )
    parser.add_argument("--who-db", type=Path, required=True)
    parser.add_argument("--ctgov-db", type=Path, required=True)
    parser.add_argument("--ctis-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--strategy", type=Path, default=PROJECT_ROOT / "config" / "who_search_strategy.yaml")
    return parser.parse_args()


def ensure_hybrid_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS trial_country_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trial_uid TEXT NOT NULL,
            country TEXT NOT NULL,
            evidence_type TEXT NOT NULL DEFAULT 'registry_country_list',
            source_name TEXT,
            source_url TEXT,
            last_verified_at TEXT,
            FOREIGN KEY(trial_uid) REFERENCES trial_master(trial_uid) ON DELETE CASCADE,
            UNIQUE(trial_uid, country, source_name)
        );
        CREATE INDEX IF NOT EXISTS idx_trial_country_records_country_trial
            ON trial_country_records(country COLLATE NOCASE, trial_uid);
        CREATE TABLE IF NOT EXISTS trial_source_provenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trial_uid TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_trial_uid TEXT NOT NULL,
            source_registry_id TEXT,
            source_fetched_at TEXT,
            merged_at TEXT NOT NULL,
            FOREIGN KEY(trial_uid) REFERENCES trial_master(trial_uid) ON DELETE CASCADE,
            UNIQUE(trial_uid, source_name, source_trial_uid)
        );
        """
    )


def migrate_country_only_sites(conn: sqlite3.Connection) -> dict[str, int]:
    predicate = """
        trim(COALESCE(site_name, '')) = ''
        AND trim(COALESCE(city, '')) = ''
        AND trim(COALESCE(province, '')) = ''
        AND trim(COALESCE(country, '')) <> ''
    """
    before = int(conn.execute(f"SELECT COUNT(*) FROM trial_sites WHERE {predicate}").fetchone()[0])
    conn.execute(
        f"""
        INSERT OR IGNORE INTO trial_country_records(
            trial_uid, country, evidence_type, source_name, source_url, last_verified_at
        )
        SELECT trial_uid, country, 'registry_country_list',
               COALESCE(source_name, 'WHO_ICTRP'), source_url, last_verified_at
        FROM trial_sites WHERE {predicate}
        """
    )
    conn.execute(f"DELETE FROM trial_sites WHERE {predicate}")
    return {"country_only_sites_migrated": before}


def _attach(conn: sqlite3.Connection, source_path: Path, alias: str) -> None:
    conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(source_path.resolve()),))


def merge_source(
    conn: sqlite3.Connection,
    source_path: Path,
    *,
    alias: str,
    source_name: str,
    copy_named_sites: bool,
    authoritative_detail: bool,
    merged_at: str,
) -> dict[str, int]:
    _attach(conn, source_path, alias)
    conn.executescript(
        """
        DROP TABLE IF EXISTS temp.target_registry_ids;
        CREATE TEMP TABLE target_registry_ids(normalized_id TEXT, trial_uid TEXT);
        INSERT INTO target_registry_ids
        SELECT upper(CASE WHEN registry_id LIKE 'CTIS%' THEN substr(registry_id, 5) ELSE registry_id END), trial_uid
        FROM main.trial_registry_ids;
        CREATE INDEX temp.idx_target_registry_ids ON target_registry_ids(normalized_id);
        DROP TABLE IF EXISTS temp.source_merge_map;
        CREATE TEMP TABLE source_merge_map(source_uid TEXT PRIMARY KEY, target_uid TEXT NOT NULL);
        """
    )
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    conn.execute(
        f"""
        INSERT INTO temp.source_merge_map(source_uid, target_uid)
        SELECT s.trial_uid,
               COALESCE(MIN(t.trial_uid), s.trial_uid)
        FROM {alias}.trial_master s
        LEFT JOIN {alias}.trial_registry_ids sri ON sri.trial_uid = s.trial_uid
        LEFT JOIN temp.target_registry_ids t
          ON t.normalized_id = upper(CASE WHEN sri.registry_id LIKE 'CTIS%' THEN substr(sri.registry_id, 5) ELSE sri.registry_id END)
        WHERE lower(COALESCE(s.cancer_recall_confidence, 'high')) = 'high'
          AND lower(COALESCE(s.recruitment_status_normalized, '')) IN ({placeholders})
        GROUP BY s.trial_uid
        """,
        ACTIVE_STATUSES,
    )
    selected = int(conn.execute("SELECT COUNT(*) FROM temp.source_merge_map").fetchone()[0])
    overlaps = int(conn.execute("SELECT COUNT(*) FROM temp.source_merge_map WHERE source_uid <> target_uid").fetchone()[0])

    master_columns = (
        "trial_uid, primary_registry_id, primary_source, title, scientific_title, brief_summary, "
        "recruitment_status_raw, recruitment_status_normalized, phase_raw, phase_normalized, "
        "study_type_raw, study_type_normalized, disease_text, disease_normalized, cancer_type_normalized, "
        "intervention_summary, sponsor_summary, countries, registration_date, start_date, completion_date, "
        "last_update_date, source_url, last_fetched_at, cancer_recall_source, cancer_recall_confidence, data_quality_status"
    )
    select_columns = master_columns.replace("trial_uid", "m.target_uid", 1)
    conn.execute(
        f"""
        INSERT OR IGNORE INTO main.trial_master({master_columns})
        SELECT {select_columns}
        FROM {alias}.trial_master s
        JOIN temp.source_merge_map m ON m.source_uid = s.trial_uid
        """
    )
    inserted = int(conn.execute("SELECT changes()").fetchone()[0])

    if authoritative_detail:
        detail_columns = (
            "title, scientific_title, brief_summary, recruitment_status_raw, recruitment_status_normalized, "
            "phase_raw, phase_normalized, study_type_raw, study_type_normalized, disease_text, disease_normalized, "
            "cancer_type_normalized, intervention_summary, sponsor_summary, countries, registration_date, "
            "start_date, completion_date, last_update_date, last_fetched_at, cancer_recall_source, "
            "cancer_recall_confidence, data_quality_status"
        )
        conn.execute(
            f"""
            UPDATE main.trial_master
            SET ({detail_columns}) = (
                SELECT {detail_columns} FROM {alias}.trial_master s
                JOIN temp.source_merge_map m ON m.source_uid = s.trial_uid
                WHERE m.target_uid = main.trial_master.trial_uid LIMIT 1
            )
            WHERE trial_uid IN (SELECT target_uid FROM temp.source_merge_map)
            """
        )

    conn.execute(
        f"""
        INSERT OR IGNORE INTO main.trial_registry_ids(
            trial_uid, registry_source, registry_id, id_type, is_primary, source_url
        )
        SELECT m.target_uid, r.registry_source, r.registry_id, r.id_type,
               CASE WHEN m.source_uid = m.target_uid THEN r.is_primary ELSE 0 END, r.source_url
        FROM {alias}.trial_registry_ids r
        JOIN temp.source_merge_map m ON m.source_uid = r.trial_uid
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO main.trial_source_provenance(
            trial_uid, source_name, source_trial_uid, source_registry_id, source_fetched_at, merged_at
        )
        SELECT m.target_uid, ?, s.trial_uid, s.primary_registry_id, s.last_fetched_at, ?
        FROM {alias}.trial_master s JOIN temp.source_merge_map m ON m.source_uid = s.trial_uid
        """,
        (source_name, merged_at),
    )

    if authoritative_detail:
        conn.execute("DELETE FROM main.trial_interventions WHERE trial_uid IN (SELECT target_uid FROM temp.source_merge_map)")
        conn.execute("DELETE FROM main.trial_eligibility_criteria WHERE trial_uid IN (SELECT target_uid FROM temp.source_merge_map)")
        conn.execute("DELETE FROM main.trial_sites WHERE trial_uid IN (SELECT target_uid FROM temp.source_merge_map)")
    intervention_columns = (
        "trial_uid, arm_name, arm_type, sample_size, intervention_name_raw, intervention_name_normalized, "
        "intervention_type, drug_name_normalized, drug_aliases, target, mechanism, therapy_class, is_combination"
    )
    conn.execute(
        f"""
        INSERT INTO main.trial_interventions({intervention_columns})
        SELECT m.target_uid, i.arm_name, i.arm_type, i.sample_size, i.intervention_name_raw,
               i.intervention_name_normalized, i.intervention_type, i.drug_name_normalized,
               i.drug_aliases, i.target, i.mechanism, i.therapy_class, i.is_combination
        FROM {alias}.trial_interventions i JOIN temp.source_merge_map m ON m.source_uid = i.trial_uid
        WHERE ? OR NOT EXISTS (
            SELECT 1 FROM main.trial_interventions current WHERE current.trial_uid = m.target_uid
        )
        """,
        (1 if authoritative_detail else 0,),
    )
    criteria_columns = (
        "trial_uid, criterion_type, criterion_text, language, criterion_order, parsed_category, "
        "is_critical, normalized_entities, source_section"
    )
    conn.execute(
        f"""
        INSERT INTO main.trial_eligibility_criteria({criteria_columns})
        SELECT m.target_uid, e.criterion_type, e.criterion_text, e.language, e.criterion_order,
               e.parsed_category, e.is_critical, e.normalized_entities, e.source_section
        FROM {alias}.trial_eligibility_criteria e JOIN temp.source_merge_map m ON m.source_uid = e.trial_uid
        WHERE ? OR NOT EXISTS (
            SELECT 1 FROM main.trial_eligibility_criteria current WHERE current.trial_uid = m.target_uid
        )
        """,
        (1 if authoritative_detail else 0,),
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO main.trial_country_records(
            trial_uid, country, evidence_type, source_name, source_url, last_verified_at
        )
        SELECT m.target_uid, s.country, 'registry_country_list', ?, MAX(s.source_url), MAX(src.last_fetched_at)
        FROM {alias}.trial_sites s
        JOIN temp.source_merge_map m ON m.source_uid = s.trial_uid
        JOIN {alias}.trial_master src ON src.trial_uid = s.trial_uid
        WHERE trim(COALESCE(s.country, '')) <> ''
        GROUP BY m.target_uid, s.country
        """,
        (source_name,),
    )
    if copy_named_sites:
        site_columns = (
            "trial_uid, site_name, country, province, city, site_status, investigator, contact_name, "
            "contact_phone, contact_email, source_name, source_url, last_verified_at"
        )
        conn.execute(
            f"""
            INSERT INTO main.trial_sites({site_columns})
            SELECT m.target_uid, s.site_name, s.country, s.province, s.city, s.site_status,
                   s.investigator, s.contact_name, s.contact_phone, s.contact_email,
                   ?, s.source_url, src.last_fetched_at
            FROM {alias}.trial_sites s
            JOIN temp.source_merge_map m ON m.source_uid = s.trial_uid
            JOIN {alias}.trial_master src ON src.trial_uid = s.trial_uid
            WHERE trim(COALESCE(s.site_name, '')) <> ''
               OR trim(COALESCE(s.city, '')) <> ''
               OR trim(COALESCE(s.province, '')) <> ''
            """,
            (source_name,),
        )
    countries = int(conn.execute(
        "SELECT COUNT(*) FROM main.trial_country_records WHERE source_name = ?", (source_name,)
    ).fetchone()[0])
    sites = int(conn.execute(
        "SELECT COUNT(*) FROM main.trial_sites WHERE source_name = ?", (source_name,)
    ).fetchone()[0])
    conn.commit()
    conn.execute(f"DETACH DATABASE {alias}")
    return {"selected": selected, "overlaps": overlaps, "inserted": inserted, "country_records": countries, "named_sites": sites}


TRUSTED_REGISTRY_ID = re.compile(
    r"^(?:NCT\d{8}|CTIS?\d{4}-\d{6}-\d{2}-\d{2}|\d{4}-\d{6}-\d{2}-\d{2}|"
    r"ChiCTR[A-Za-z0-9-]+|ISRCTN\d+|ACTRN[A-Za-z0-9/]+|DRKS\d+|JPRN-[A-Za-z0-9-]+|"
    r"UMIN[A-Za-z0-9-]+|CTRI/[A-Za-z0-9/]+|NL-OMON\d+|IRCT[A-Za-z0-9-]+)$",
    re.IGNORECASE,
)


def _normalized_registry_id(value: str) -> str:
    value = str(value or "").strip().upper()
    return value[4:] if value.startswith("CTIS") else value


def deduplicate_registry_records(conn: sqlite3.Connection) -> dict[str, int]:
    """Merge records only when they share a registry-shaped identifier.

    Free-text secondary IDs such as "none", "pending", or sponsor protocol
    labels are deliberately excluded because they are not globally unique.
    """
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    seen: dict[str, str] = {}
    for trial_uid, registry_id in conn.execute("SELECT trial_uid, registry_id FROM trial_registry_ids"):
        raw = str(registry_id or "").strip()
        if not TRUSTED_REGISTRY_ID.fullmatch(raw):
            continue
        normalized = _normalized_registry_id(raw)
        if normalized in seen:
            union(seen[normalized], trial_uid)
        else:
            seen[normalized] = trial_uid
            find(trial_uid)
    groups: dict[str, list[str]] = {}
    for trial_uid in parent:
        groups.setdefault(find(trial_uid), []).append(trial_uid)
    duplicate_groups = [sorted(set(values)) for values in groups.values() if len(set(values)) > 1]

    def canonical_key(uid: str) -> tuple[int, int, str]:
        row = conn.execute(
            "SELECT primary_registry_id, primary_source FROM trial_master WHERE trial_uid = ?", (uid,)
        ).fetchone()
        primary_id, primary_source = row if row else ("", "")
        id_priority = 0 if str(primary_id).upper().startswith("NCT") else 1 if str(primary_id).upper().startswith("CTIS") else 2
        source_priority = 0 if str(primary_source).casefold() in {"clinicaltrials.gov", "ctgov"} else 1
        return id_priority, source_priority, uid

    merged_records = 0
    for group in duplicate_groups:
        canonical = min(group, key=canonical_key)
        for duplicate in group:
            if duplicate == canonical:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO trial_registry_ids(
                       trial_uid,registry_source,registry_id,id_type,is_primary,source_url)
                   SELECT ?,registry_source,registry_id,id_type,0,source_url
                   FROM trial_registry_ids WHERE trial_uid=?""",
                (canonical, duplicate),
            )
            conn.execute(
                """INSERT OR IGNORE INTO trial_country_records(
                       trial_uid,country,evidence_type,source_name,source_url,last_verified_at)
                   SELECT ?,country,evidence_type,source_name,source_url,last_verified_at
                   FROM trial_country_records WHERE trial_uid=?""",
                (canonical, duplicate),
            )
            conn.execute(
                """INSERT OR IGNORE INTO trial_source_provenance(
                       trial_uid,source_name,source_trial_uid,source_registry_id,source_fetched_at,merged_at)
                   SELECT ?,source_name,source_trial_uid,source_registry_id,source_fetched_at,merged_at
                   FROM trial_source_provenance WHERE trial_uid=?""",
                (canonical, duplicate),
            )
            for table in ("trial_sites", "trial_interventions", "trial_eligibility_criteria"):
                conn.execute(f"UPDATE {table} SET trial_uid=? WHERE trial_uid=?", (canonical, duplicate))
            for table in ("trial_registry_ids", "trial_country_records", "trial_source_provenance"):
                conn.execute(f"DELETE FROM {table} WHERE trial_uid=?", (duplicate,))
            conn.execute("DELETE FROM trial_master WHERE trial_uid=?", (duplicate,))
            merged_records += 1
    return {"duplicate_groups": len(duplicate_groups), "merged_records": merged_records}


def remove_empty_site_rows(conn: sqlite3.Connection) -> int:
    predicate = """
        trim(COALESCE(site_name, '')) = ''
        AND trim(COALESCE(city, '')) = ''
        AND trim(COALESCE(province, '')) = ''
    """
    count = int(conn.execute(f"SELECT COUNT(*) FROM trial_sites WHERE {predicate}").fetchone()[0])
    conn.execute(f"DELETE FROM trial_sites WHERE {predicate}")
    return count

def main() -> None:
    args = parse_args()
    for path in (args.who_db, args.ctgov_db, args.ctis_db):
        if not path.exists():
            raise SystemExit(f"Source database not found: {path}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.resolve() != args.who_db.resolve():
        shutil.copy2(args.who_db, args.out)
    conn = sqlite3.connect(args.out)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    merged_at = utc_now()
    try:
        ensure_hybrid_schema(conn)
        report: dict[str, Any] = {"merged_at": merged_at, "country_migration": migrate_country_only_sites(conn)}
        report["clinicaltrials_gov"] = merge_source(
            conn, args.ctgov_db, alias="ctgov", source_name="ClinicalTrials.gov",
            copy_named_sites=True, authoritative_detail=True, merged_at=merged_at,
        )
        conn.commit()
        report["eu_ctis"] = merge_source(
            conn, args.ctis_db, alias="ctis", source_name="EU_CTIS",
            copy_named_sites=False, authoritative_detail=False, merged_at=merged_at,
        )
        conn.commit()
        report["registry_deduplication"] = deduplicate_registry_records(conn)
        report["empty_site_rows_removed"] = remove_empty_site_rows(conn)
        conn.commit()
        optimization = optimize_database(
            conn,
            built_at=merged_at,
            strategy_path=args.strategy,
            coverage_scope={
                "sources": ["WHO ICTRP", "ClinicalTrials.gov", "EU CTIS"],
                "statuses": list(ACTIVE_STATUSES),
                "location_model": "named_sites_plus_registry_country_records",
            },
        )
        report["optimization"] = optimization
        conn.execute(
            "INSERT OR REPLACE INTO database_metadata(key,value,updated_at) VALUES ('schema_version','3',?)",
            (merged_at,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO database_metadata(key,value,updated_at) VALUES ('source_watermarks',?,?)",
            (json.dumps({
                "WHO_ICTRP": conn.execute("SELECT MAX(last_fetched_at) FROM trial_master WHERE primary_source='WHO_ICTRP'").fetchone()[0],
                "ClinicalTrials.gov": conn.execute("SELECT MAX(source_fetched_at) FROM trial_source_provenance WHERE source_name='ClinicalTrials.gov'").fetchone()[0],
                "EU_CTIS": conn.execute("SELECT MAX(source_fetched_at) FROM trial_source_provenance WHERE source_name='EU_CTIS'").fetchone()[0],
            }, ensure_ascii=False), merged_at),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        report["final"] = {
            "trials": conn.execute("SELECT COUNT(*) FROM trial_master").fetchone()[0],
            "registry_ids": conn.execute("SELECT COUNT(*) FROM trial_registry_ids").fetchone()[0],
            "country_records": conn.execute("SELECT COUNT(*) FROM trial_country_records").fetchone()[0],
            "named_sites": conn.execute("SELECT COUNT(*) FROM trial_sites").fetchone()[0],
            "integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
        }
    finally:
        conn.close()
    report_path = args.out.with_suffix(args.out.suffix + ".build-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
