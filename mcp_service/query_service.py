from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_STATUSES = ["recruiting"]
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        yield conn
    finally:
        conn.close()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')", (name,)
    ).fetchone() is not None


def _metadata_value(key: str, value: str) -> Any:
    if key in {"coverage_scope", "source_watermarks"}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if key in {"fts_row_count", "country_classifiable_trials"}:
        try:
            return int(value)
        except ValueError:
            return value
    return value


def database_metadata(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        metadata: dict[str, Any] = {}
        if _table_exists(conn, "database_metadata"):
            for row in conn.execute("SELECT key, value FROM database_metadata"):
                metadata[row["key"]] = _metadata_value(row["key"], row["value"])
        if not metadata.get("source_searched_through") and _table_exists(conn, "who_search_runs"):
            metadata["source_searched_through"] = conn.execute(
                "SELECT MAX(searched_at) FROM who_search_runs"
            ).fetchone()[0]
        if not metadata.get("max_fetched_at"):
            metadata["max_fetched_at"] = conn.execute(
                "SELECT MAX(last_fetched_at) FROM trial_master"
            ).fetchone()[0]
        metadata["db_path"] = str(db_path)
        source_watermarks = metadata.get("source_watermarks") or {}
        valid_watermarks = [str(value) for value in source_watermarks.values() if value]
        metadata["database_as_of"] = (
            max(valid_watermarks)
            if valid_watermarks
            else metadata.get("source_searched_through")
            or metadata.get("max_fetched_at")
            or metadata.get("database_built_at")
        )
        return metadata


def database_summary(db_path: Path) -> dict[str, Any]:
    metadata = database_metadata(db_path)
    with connect(db_path) as conn:
        counts: dict[str, int] = {}
        if _table_exists(conn, "database_table_stats"):
            counts = {
                row["table_name"]: int(row["row_count"])
                for row in conn.execute("SELECT table_name, row_count FROM database_table_stats")
            }
        if not counts:
            for table in (
                "trial_master", "trial_registry_ids", "trial_interventions",
                "trial_eligibility_criteria", "trial_sites", "trial_country_records",
                "trial_source_provenance", "who_search_runs",
                "who_trial_records", "who_recall_hits",
            ):
                if _table_exists(conn, table):
                    counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        classifiable = int(metadata.get("country_classifiable_trials") or 0)
        if not classifiable:
            country_table = "trial_country_records" if _table_exists(conn, "trial_country_records") else "trial_sites"
            classifiable = int(conn.execute(f"SELECT COUNT(DISTINCT trial_uid) FROM {country_table} WHERE trim(COALESCE(country, '')) <> ''").fetchone()[0])
        total = counts.get("trial_master", 0)
        return {
            "database_as_of": metadata.get("database_as_of"),
            "database_built_at": metadata.get("database_built_at"),
            "source_searched_through": metadata.get("source_searched_through"),
            "max_fetched_at": metadata.get("max_fetched_at"),
            "schema_version": metadata.get("schema_version"),
            "search_index_version": metadata.get("search_index_version"),
            "counts": counts,
            "country_classifiable_trials": classifiable,
            "country_classifiable_pct": round(classifiable * 100 / total, 2) if total else 0,
        }


def _clean_terms(terms: list[str] | None) -> list[str]:
    return [str(term).strip() for term in (terms or []) if str(term).strip()]


def _term_expression(term: str) -> str:
    tokens = _TOKEN_RE.findall(term)
    if not tokens:
        raise ValueError(f"Search term has no searchable tokens: {term!r}")
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _dimension_expression(terms: list[str], columns: list[str] | None = None) -> str:
    alternatives = " OR ".join(f"({_term_expression(term)})" for term in terms)
    grouped = f"({alternatives})"
    if columns:
        return "(" + " OR ".join(f"{column} : {grouped}" for column in columns) + ")"
    return grouped


def build_fts_query(
    *,
    general_terms: list[str] | None = None,
    condition_terms: list[str] | None = None,
    biomarker_terms: list[str] | None = None,
    intervention_terms: list[str] | None = None,
    eligibility_terms: list[str] | None = None,
) -> tuple[str, list[str]]:
    dimensions: list[tuple[str, list[str], list[str] | None]] = [
        ("general", _clean_terms(general_terms), None),
        ("condition", _clean_terms(condition_terms), ["title", "scientific_title", "brief_summary", "disease_text"]),
        ("biomarker", _clean_terms(biomarker_terms), None),
        ("intervention", _clean_terms(intervention_terms), ["intervention_text"]),
        ("eligibility", _clean_terms(eligibility_terms), ["eligibility_text"]),
    ]
    active = [(name, terms, columns) for name, terms, columns in dimensions if terms]
    if not active:
        return "", []
    return " AND ".join(_dimension_expression(terms, columns) for _, terms, columns in active), [
        name for name, _, _ in active
    ]


def search_trials_multidimensional(
    db_path: Path,
    *,
    general_terms: list[str] | None = None,
    condition_terms: list[str] | None = None,
    biomarker_terms: list[str] | None = None,
    intervention_terms: list[str] | None = None,
    eligibility_terms: list[str] | None = None,
    country: str = "",
    recruitment_statuses: list[str] | None = None,
    interventional_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, min(int(offset), 10000))
    country = country.strip()
    statuses = _clean_terms(recruitment_statuses) or DEFAULT_STATUSES
    fts_query, matched_dimensions = build_fts_query(
        general_terms=general_terms,
        condition_terms=condition_terms,
        biomarker_terms=biomarker_terms,
        intervention_terms=intervention_terms,
        eligibility_terms=eligibility_terms,
    )
    with connect(db_path) as conn:
        if fts_query and not _table_exists(conn, "trial_search_fts"):
            raise RuntimeError("Search index is missing. Run scripts/matching_db.py before starting MCP.")
        filter_params: list[Any] = []
        joins = ""
        clauses: list[str] = []
        rank = "0.0 AS search_rank"
        if fts_query:
            joins = "JOIN trial_search_fts ON trial_search_fts.trial_uid = tm.trial_uid"
            clauses.append("trial_search_fts MATCH ?")
            filter_params.append(fts_query)
            rank = "bm25(trial_search_fts, 0.0, 2.0, 2.0, 0.5, 2.5, 2.0, 1.5) AS search_rank"
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"tm.recruitment_status_normalized IN ({placeholders})")
        filter_params.extend(statuses)
        if interventional_only:
            clauses.append(
                "(LOWER(TRIM(COALESCE(tm.study_type_normalized, ''))) LIKE 'intervention%' "
                "OR LOWER(TRIM(COALESCE(tm.study_type_normalized, ''))) = 'treatment study')"
            )
        if country:
            clauses.append(
                "(EXISTS (SELECT 1 FROM trial_sites ts WHERE ts.trial_uid = tm.trial_uid "
                "AND ts.country = ? COLLATE NOCASE) OR EXISTS (SELECT 1 FROM trial_country_records tc "
                "WHERE tc.trial_uid = tm.trial_uid AND tc.country = ? COLLATE NOCASE))"
            )
            filter_params.extend([country, country])
        where = " AND ".join(clauses)
        params = [country, country, country, country, *filter_params, limit, offset]
        sql = f"""
            SELECT tm.trial_uid, tm.primary_registry_id, tm.primary_source, tm.title,
                   tm.scientific_title, tm.brief_summary, tm.recruitment_status_normalized,
                   tm.phase_normalized, tm.study_type_normalized, tm.disease_text,
                   tm.intervention_summary, tm.sponsor_summary, tm.countries,
                   tm.registration_date, tm.last_update_date, tm.last_fetched_at,
                   tm.source_url, tm.cancer_recall_confidence,
                   (SELECT COUNT(*) FROM trial_sites sc
                    WHERE sc.trial_uid = tm.trial_uid
                      AND (? = '' OR sc.country = ? COLLATE NOCASE)) AS matching_site_count,
                   (SELECT COUNT(*) FROM trial_country_records cr
                    WHERE cr.trial_uid = tm.trial_uid
                      AND (? = '' OR cr.country = ? COLLATE NOCASE)) AS matching_country_record_count,
                   {rank}
            FROM trial_master tm
            {joins}
            WHERE {where}
            ORDER BY search_rank,
                     CASE tm.cancer_recall_confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     tm.registration_date DESC
            LIMIT ? OFFSET ?
        """
        results = rows_to_dicts(conn.execute(sql, params).fetchall())
    for result in results:
        result["matched_dimensions"] = matched_dimensions
    metadata = database_metadata(db_path)
    return {
        "results": results,
        "query": {
            "fts": fts_query,
            "general_terms": _clean_terms(general_terms),
            "condition_terms": _clean_terms(condition_terms),
            "biomarker_terms": _clean_terms(biomarker_terms),
            "intervention_terms": _clean_terms(intervention_terms),
            "eligibility_terms": _clean_terms(eligibility_terms),
            "country": country,
            "recruitment_statuses": statuses,
            "interventional_only": bool(interventional_only),
        },
        "pagination": {"limit": limit, "offset": offset, "returned": len(results)},
        "database_as_of": metadata.get("database_as_of"),
    }


def _split_criteria_text(text: str) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {"inclusion": [], "exclusion": [], "unknown": []}
    current = "unknown"
    for raw_line in re.split(r"[\r\n]+", text or ""):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower().rstrip(":")
        if "inclusion" in lowered and "criteria" in lowered:
            current = "inclusion"
            continue
        if "exclusion" in lowered and "criteria" in lowered:
            current = "exclusion"
            continue
        line = re.sub(r"^(?:[-*\u2022]+|\d+[.):])\s*", "", line).strip()
        if line:
            output[current].append(line)
    return output


def _parsed_criteria(rows: list[dict[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, list[str]] = {"inclusion": [], "exclusion": [], "unknown": []}
    raw_parts: list[str] = []
    for row in rows:
        text = str(row.get("criterion_text") or "").strip()
        if not text:
            continue
        raw_parts.append(text)
        criterion_type = str(row.get("criterion_type") or "unknown")
        if criterion_type in {"inclusion", "exclusion"}:
            parsed[criterion_type].append(text)
        else:
            split = _split_criteria_text(text)
            for key in parsed:
                parsed[key].extend(split[key])
    return {**parsed, "raw": "\n".join(raw_parts)}


def get_trial(db_path: Path, registry_id: str) -> dict[str, Any]:
    value = registry_id.strip()
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM trial_master WHERE trial_uid = ? LIMIT 1", (value,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM trial_master WHERE primary_registry_id = ? COLLATE NOCASE LIMIT 1", (value,)
            ).fetchone()
        if row is None:
            row = conn.execute(
                """SELECT tm.* FROM trial_registry_ids tri
                   JOIN trial_master tm ON tm.trial_uid = tri.trial_uid
                   WHERE tri.registry_id = ? COLLATE NOCASE LIMIT 1""",
                (value,),
            ).fetchone()
        if row is None:
            return {"found": False, "query": value}
        trial = dict(row)
        trial_uid = trial["trial_uid"]
        trial["registry_ids"] = rows_to_dicts(conn.execute(
            "SELECT registry_source, registry_id, id_type, is_primary, source_url FROM trial_registry_ids WHERE trial_uid = ?",
            (trial_uid,),
        ).fetchall())
        trial["sites"] = rows_to_dicts(conn.execute(
            """SELECT country, province, city, site_name, site_status, investigator,
                      contact_name, contact_phone, contact_email, source_url, last_verified_at
               FROM trial_sites WHERE trial_uid = ?""",
            (trial_uid,),
        ).fetchall())
        trial["country_records"] = rows_to_dicts(conn.execute(
            """SELECT country, evidence_type, source_name, source_url, last_verified_at
               FROM trial_country_records WHERE trial_uid = ? ORDER BY country""",
            (trial_uid,),
        ).fetchall())
        trial["interventions"] = rows_to_dicts(conn.execute(
            """SELECT intervention_name_raw, intervention_name_normalized, intervention_type,
                      target, mechanism, therapy_class
               FROM trial_interventions WHERE trial_uid = ?""",
            (trial_uid,),
        ).fetchall())
        trial["criteria"] = rows_to_dicts(conn.execute(
            """SELECT criterion_type, criterion_text, language, criterion_order,
                      parsed_category, is_critical, source_section
               FROM trial_eligibility_criteria WHERE trial_uid = ? ORDER BY criterion_order, id""",
            (trial_uid,),
        ).fetchall())
        trial["parsed_criteria"] = _parsed_criteria(trial["criteria"])
        trial["found"] = True
        return trial


def country_stats(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 300))
    with connect(db_path) as conn:
        return rows_to_dicts(conn.execute(
            """SELECT country, COUNT(DISTINCT trial_uid) AS trials FROM (
                   SELECT trial_uid, country FROM trial_sites WHERE trim(COALESCE(country, '')) <> ''
                   UNION ALL
                   SELECT trial_uid, country FROM trial_country_records WHERE trim(COALESCE(country, '')) <> ''
               ) GROUP BY country ORDER BY trials DESC, country LIMIT ?""",
            (limit,),
        ).fetchall())


