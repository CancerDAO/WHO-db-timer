from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from eligibility_parser import split_eligibility_criteria
from matching_db import ensure_matching_schema, optimize_database
from who_common import (
    ADVANCED_SEARCH_URL,
    DATA_DIR,
    DEFAULT_DB,
    PARSER_VERSION,
    REPORT_DIR,
    SCHEMA_PATH,
    SOURCE_NAME,
    TRIAL_URL,
    TermMatcher,
    compact_json,
    load_term_config,
    normalize_status,
    normalize_text,
    source_from_trial_id,
    split_lines,
    utc_now,
)

RAW_XML_DIR = DATA_DIR / "raw_xml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a WHO ICTRP cancer trial SQLite database from the official XML export."
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--terms", type=Path, default=Path("config/cancer_recall_terms.yaml"))
    parser.add_argument("--field", choices=["title", "condition", "intervention", "title_condition"], default="title_condition")
    parser.add_argument("--term", default="core_cancer_terms", help="Logical query label used for filenames/audit rows.")
    parser.add_argument("--title-term", default="", help="Optional title query text. Defaults from --term for title searches.")
    parser.add_argument("--condition-term", default="", help="Optional condition query text. Defaults from --term for condition searches.")
    parser.add_argument("--intervention-term", default="", help="Optional intervention query text. Defaults from --term for intervention searches.")
    parser.add_argument("--date-start", default="", help="Optional WHO date-of-registration start, dd/mm/yyyy.")
    parser.add_argument("--date-end", default="", help="Optional WHO date-of-registration end, dd/mm/yyyy.")
    parser.add_argument("--recruitment-status", choices=["recruiting", "all"], default="recruiting")
    parser.add_argument("--xml", type=Path, help="Use an existing WHO ICTRP XML export instead of downloading.")
    parser.add_argument("--download-only", action="store_true", help="Download XML and stop before database import.")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--chrome-exe", default="", help="Optional Chrome/Edge executable. Defaults to common Windows paths.")
    parser.add_argument("--search-timeout-ms", type=int, default=180000)
    parser.add_argument("--download-timeout-ms", type=int, default=900000)
    return parser.parse_args()


def chrome_candidates() -> list[Path]:
    return [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]


def find_chrome(explicit: str = "") -> Path | None:
    if explicit:
        path = Path(explicit)
        if path.exists():
            return path
        raise FileNotFoundError(path)
    for path in chrome_candidates():
        if path.exists():
            return path
    # No system browser is required when `playwright install chromium` has
    # installed Playwright's managed executable for this Python environment.
    return None


def connect(path: Path, reset: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if reset:
        for candidate in [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]:
            if candidate.exists():
                candidate.unlink()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS who_search_runs (
            search_id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_name TEXT NOT NULL,
            term TEXT NOT NULL,
            total_records INTEGER,
            total_trials INTEGER,
            searched_at TEXT NOT NULL,
            raw_result_summary TEXT,
            recruitment_status TEXT,
            query_json TEXT,
            xml_path TEXT
        );
        CREATE TABLE IF NOT EXISTS who_trial_records (
            trial_id TEXT PRIMARY KEY,
            source_register TEXT,
            detail_fetched INTEGER NOT NULL DEFAULT 1,
            search_hit_json TEXT,
            detail_json TEXT,
            source_url TEXT,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS who_recall_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trial_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            term TEXT NOT NULL,
            hit_type TEXT NOT NULL,
            confidence TEXT NOT NULL,
            UNIQUE(trial_id, field_name, term, hit_type)
        );
        CREATE TABLE IF NOT EXISTS who_download_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER NOT NULL,
            trial_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            term TEXT NOT NULL,
            xml_path TEXT,
            UNIQUE(search_id, trial_id)
        );
        """
    )
    search_columns = {row[1] for row in conn.execute("PRAGMA table_info(who_search_runs)")}
    if "xml_path" not in search_columns:
        conn.execute("ALTER TABLE who_search_runs ADD COLUMN xml_path TEXT")
    if "recruitment_status" not in search_columns:
        conn.execute("ALTER TABLE who_search_runs ADD COLUMN recruitment_status TEXT")
    if "query_json" not in search_columns:
        conn.execute("ALTER TABLE who_search_runs ADD COLUMN query_json TEXT")
    ensure_matching_schema(conn)
    return conn


def parse_count(text: str) -> tuple[int | None, int | None, str]:
    match = re.search(r"([\d,]+)\s+records\s+for\s+([\d,]+)\s+trials\s+found", text, re.I)
    if not match:
        return None, None, ""
    return int(match.group(1).replace(",", "")), int(match.group(2).replace(",", "")), match.group(0)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())[:80].strip("_")
    return slug or "query"


def recruitment_value(status: str) -> str:
    return "1" if status == "recruiting" else "ALL"


def search_terms(args: argparse.Namespace) -> dict[str, str]:
    title_term = (getattr(args, "title_term", "") or "").strip()
    condition_term = (getattr(args, "condition_term", "") or "").strip()
    intervention_term = (getattr(args, "intervention_term", "") or "").strip()
    term = (getattr(args, "term", "") or "").strip()
    if args.field == "title":
        title_term = title_term or term
    elif args.field == "condition":
        condition_term = condition_term or term
    elif args.field == "intervention":
        intervention_term = intervention_term or term
    elif args.field == "title_condition":
        title_term = title_term or term
        condition_term = condition_term or term
    return {"title": title_term, "condition": condition_term, "intervention": intervention_term}


def fill_search(page, args: argparse.Namespace) -> tuple[int | None, int | None, str]:
    timeout_ms = args.search_timeout_ms
    terms = search_terms(args)
    page.goto(ADVANCED_SEARCH_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    if terms["title"]:
        page.fill("#ctl00_ContentPlaceHolder1_txtTitle", terms["title"])
        page.select_option("#ctl00_ContentPlaceHolder1_ddlTitle", "OperatorNone")
    if terms["condition"]:
        page.select_option(
            "#ctl00_ContentPlaceHolder1_ddlOperatorCondition",
            "OperatorOR" if terms["title"] else "OperatorAND",
        )
        page.fill("#ctl00_ContentPlaceHolder1_txtCondition", terms["condition"])
    if terms["intervention"]:
        page.select_option(
            "#ctl00_ContentPlaceHolder1_ddlOperatorIntervention",
            "OperatorOR" if (terms["title"] or terms["condition"]) else "OperatorAND",
        )
        page.fill("#ctl00_ContentPlaceHolder1_txtIntervention", terms["intervention"])
    if getattr(args, "date_start", ""):
        page.fill("#ctl00_ContentPlaceHolder1_txtDateStart", args.date_start)
    if getattr(args, "date_end", ""):
        page.fill("#ctl00_ContentPlaceHolder1_txtDateEnd", args.date_end)
    page.select_option("#ctl00_ContentPlaceHolder1_ddlRecruitingStatus", recruitment_value(args.recruitment_status))
    page.click("#ctl00_ContentPlaceHolder1_btnSearch")
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1500)
    body = page.locator("body").inner_text(timeout=30000)
    return parse_count(body)

def click_attached_control(page, selector: str, timeout_ms: int) -> None:
    """Trigger a WHO WebForms control even when its modal leaves it hidden."""
    locator = page.locator(selector)
    locator.wait_for(state="attached", timeout=timeout_ms)
    locator.evaluate("element => element.click()")

def download_xml(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    RAW_XML_DIR.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome(args.chrome_exe)
    exported_at = utc_now().replace(":", "").replace("+", "Z")
    xml_path = RAW_XML_DIR / f"who_ictrp_{args.recruitment_status}_{args.field}_{safe_slug(args.term)}_{exported_at}.xml"
    partial_path = xml_path.with_suffix(xml_path.suffix + ".part")
    with sync_playwright() as p:
        launch_options = {
            "headless": not args.headful,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if chrome is not None:
            launch_options["executable_path"] = str(chrome)
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context(
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(args.search_timeout_ms)
        page.on("dialog", lambda dialog: dialog.accept())
        total_records, total_trials, summary = fill_search(page, args)
        click_attached_control(
            page, "#ctl00_ContentPlaceHolder1_btnLaunchDialogTerms", args.search_timeout_ms
        )
        click_attached_control(page, "#ctl00_ContentPlaceHolder1_btnExport", args.search_timeout_ms)
        with page.expect_download(timeout=args.download_timeout_ms) as download_info:
            click_attached_control(
                page,
                "#ctl00_ContentPlaceHolder1_ucExportDefault_butExportAllTrials",
                args.search_timeout_ms,
            )
        download = download_info.value
        download.save_as(str(partial_path))
        context.close()
        browser.close()
    validation = validate_xml_export(partial_path, expected_trials=total_trials)
    if not validation["valid"]:
        partial_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"WHO XML export validation failed for {args.term}: "
            f"{validation['error']}"
        )
    partial_path.replace(xml_path)
    return xml_path, {
        "field": args.field,
        "term": args.term,
        "total_records": total_records,
        "total_trials": total_trials,
        "summary": summary,
        "xml_path": str(xml_path),
        "bytes": xml_path.stat().st_size,
        "validated_trial_records": validation["trial_records"],
        "xml_well_formed": validation["well_formed"],
    }


def clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def trial_to_record(node: ET.Element) -> dict[str, Any]:
    raw = {child.tag: clean_text(child.text) for child in node}
    trial_id = raw.get("TrialID", "").strip()
    countries = split_lines((raw.get("Countries") or "").replace(";", "\n"))
    secondary_ids = split_lines((raw.get("Secondary_ID") or "").replace(";", "\n"))
    record = {
        "trial_id": trial_id,
        "internal_number": raw.get("Internal_Number", ""),
        "export_date": raw.get("Export_date", ""),
        "last_refreshed": raw.get("Last_Refreshed_on", ""),
        "public_title": raw.get("Public_title", ""),
        "scientific_title": raw.get("Scientific_title", ""),
        "acronym": raw.get("Acronym", ""),
        "primary_sponsor": raw.get("Primary_sponsor", ""),
        "secondary_sponsor": raw.get("Secondary_Sponsor", ""),
        "prospective_registration": raw.get("Prospective_registration", ""),
        "date_registration": raw.get("Date_registration", "") or raw.get("Date_registration3", ""),
        "date_registration_sort": raw.get("Date_registration3", ""),
        "register": raw.get("Source_Register", ""),
        "source_url": raw.get("web_address", ""),
        "recruitment_status": raw.get("Recruitment_Status", ""),
        "date_first_enrolment": raw.get("Date_enrollement", ""),
        "target_sample_size": raw.get("Target_size", ""),
        "study_type": raw.get("Study_type", ""),
        "study_design": raw.get("Study_design", ""),
        "phase": raw.get("Phase", ""),
        "conditions": split_lines((raw.get("Condition") or "").replace(";", "\n")),
        "interventions": split_lines((raw.get("Intervention") or "").replace(";", "\n")),
        "primary_outcome": raw.get("Primary_outcome", ""),
        "secondary_outcome": raw.get("Secondary_outcome", ""),
        "criteria": "\n".join(
            part for part in [raw.get("Inclusion_Criteria", ""), raw.get("Exclusion_Criteria", "")] if part
        ),
        "inclusion_agemin": raw.get("Inclusion_agemin", ""),
        "inclusion_agemax": raw.get("Inclusion_agemax", ""),
        "inclusion_gender": raw.get("Inclusion_gender", ""),
        "countries": countries,
        "secondary_ids": secondary_ids,
        "contact_name": " ".join(part for part in [raw.get("Contact_Firstname", ""), raw.get("Contact_Lastname", "")] if part),
        "contact_email": raw.get("Contact_Email", ""),
        "contact_phone": raw.get("Contact_Tel", ""),
        "raw": raw,
    }
    if not record["source_url"] and trial_id:
        record["source_url"] = TRIAL_URL.format(trial_id=trial_id)
    return record


def iter_xml_records(path: Path):
    try:
        for event, elem in ET.iterparse(path, events=("end",)):
            if elem.tag != "Trial":
                continue
            record = trial_to_record(elem)
            elem.clear()
            if record["trial_id"]:
                yield record
    except ET.ParseError as exc:
        # Large WHO exports sometimes contain all complete <Trial> blocks but miss the final root close tag.
        # In that specific case, keep the complete records already yielded and let quality checks flag counts.
        if "no element found" in str(exc).lower():
            return
        raise


def validate_xml_export(path: Path, expected_trials: int | None = None) -> dict[str, Any]:
    """Reject empty, unreadable, or silently incomplete WHO exports."""
    if not path.exists() or path.stat().st_size == 0:
        return {"valid": False, "trial_records": 0, "well_formed": False, "error": "empty export"}
    trial_records = 0
    try:
        for _ in iter_xml_records(path):
            trial_records += 1
    except (ET.ParseError, OSError) as exc:
        return {
            "valid": False,
            "trial_records": trial_records,
            "well_formed": False,
            "error": f"unreadable XML: {exc}",
        }
    if trial_records == 0:
        return {"valid": False, "trial_records": 0, "well_formed": True, "error": "no Trial records"}
    if expected_trials is not None and trial_records < expected_trials:
        return {
            "valid": False,
            "trial_records": trial_records,
            "well_formed": True,
            "error": f"incomplete export: expected at least {expected_trials}, found {trial_records}",
        }
    return {"valid": True, "trial_records": trial_records, "well_formed": True, "error": ""}

def recall_record(record: dict[str, Any], hard: TermMatcher, context: TermMatcher, review: TermMatcher) -> dict[str, Any]:
    fields = {
        "title": " ".join([record.get("public_title", ""), record.get("scientific_title", ""), record.get("acronym", "")]),
        "condition": " | ".join(record.get("conditions", [])),
        "intervention": " | ".join(record.get("interventions", [])),
        "outcome": " ".join([record.get("primary_outcome", ""), record.get("secondary_outcome", "")]),
        "criteria": record.get("criteria", ""),
    }
    hard_hits: list[dict[str, str]] = []
    context_hits: list[dict[str, str]] = []
    review_hits: list[dict[str, str]] = []
    for field, text in fields.items():
        for term in hard.match(text):
            hard_hits.append({"field": field, "term": term})
        for term in context.match(text):
            context_hits.append({"field": field, "term": term})
        for term in review.match(text):
            review_hits.append({"field": field, "term": term})
    if hard_hits:
        confidence = "high" if any(hit["field"] in {"condition", "title"} for hit in hard_hits) else "medium"
        reason = "hard_term"
    elif context_hits:
        confidence = "medium" if any(hit["field"] in {"condition", "title"} for hit in context_hits) else "low"
        reason = "context_term"
    elif review_hits:
        confidence = "low"
        reason = "review_term"
    else:
        confidence = "none"
        reason = "no_cancer_signal"
    return {
        "confidence": confidence,
        "reason": reason,
        "hard_hits": hard_hits,
        "context_hits": context_hits,
        "review_hits": review_hits,
    }


def insert_trial(conn: sqlite3.Connection, record: dict[str, Any], recall: dict[str, Any], imported_at: str) -> None:
    trial_id = record["trial_id"]
    trial_uid = f"who_ictrp:{trial_id}"
    source_url = record.get("source_url") or TRIAL_URL.format(trial_id=trial_id)
    title = record.get("public_title", "")
    scientific_title = record.get("scientific_title", "") or title
    conditions = " | ".join(record.get("conditions", []))
    interventions = " | ".join(record.get("interventions", []))
    outcomes = " | ".join(part for part in [record.get("primary_outcome", ""), record.get("secondary_outcome", "")] if part)
    countries = " | ".join(record.get("countries", []))
    for child_table in ["trial_interventions", "trial_eligibility_criteria", "trial_sites", "trial_country_records"]:
        conn.execute(f"DELETE FROM {child_table} WHERE trial_uid = ?", (trial_uid,))
    conn.execute(
        """
        INSERT INTO raw_trial_records
            (source_name, source_trial_id, source_url, raw_json, fetched_at, parser_version, fetch_status)
        SELECT ?, ?, ?, ?, ?, ?, 'success'
        WHERE NOT EXISTS (
            SELECT 1 FROM raw_trial_records WHERE source_name = ? AND source_trial_id = ?
        )
        """,
        (SOURCE_NAME, trial_id, source_url, compact_json(record), imported_at, PARSER_VERSION, SOURCE_NAME, trial_id),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO trial_master (
            trial_uid, primary_registry_id, primary_source, title, scientific_title, brief_summary,
            recruitment_status_raw, recruitment_status_normalized, phase_raw, phase_normalized,
            study_type_raw, study_type_normalized, disease_text, disease_normalized, cancer_type_normalized,
            intervention_summary, sponsor_summary, countries, registration_date, start_date,
            last_update_date, source_url, last_fetched_at, cancer_recall_source,
            cancer_recall_confidence, data_quality_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trial_uid,
            trial_id,
            SOURCE_NAME,
            title,
            scientific_title,
            outcomes or record.get("study_design", ""),
            record.get("recruitment_status", ""),
            normalize_status(record.get("recruitment_status", "")),
            record.get("phase", ""),
            record.get("phase", ""),
            record.get("study_type", ""),
            record.get("study_type", ""),
            conditions,
            conditions,
            conditions,
            interventions,
            record.get("primary_sponsor", ""),
            countries,
            record.get("date_registration", ""),
            record.get("date_first_enrolment", ""),
            record.get("last_refreshed", ""),
            source_url,
            imported_at,
            recall["reason"],
            recall["confidence"],
            "needs_review" if recall["confidence"] in {"medium", "low"} else "unreviewed",
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO trial_registry_ids
            (trial_uid, registry_source, registry_id, id_type, is_primary, source_url)
        VALUES (?, ?, ?, 'main_id', 1, ?)
        """,
        (trial_uid, record.get("register") or source_from_trial_id(trial_id), trial_id, source_url),
    )
    for secondary_id in record.get("secondary_ids", []):
        if secondary_id and normalize_text(secondary_id) != normalize_text(trial_id):
            conn.execute(
                """
                INSERT OR IGNORE INTO trial_registry_ids
                    (trial_uid, registry_source, registry_id, id_type, is_primary, source_url)
                VALUES (?, ?, ?, 'secondary_id', 0, ?)
                """,
                (trial_uid, "secondary", secondary_id, source_url),
            )
    for intervention in record.get("interventions", []):
        conn.execute(
            """
            INSERT INTO trial_interventions
                (trial_uid, intervention_name_raw, intervention_type)
            VALUES (?, ?, 'unknown')
            """,
            (trial_uid, intervention),
        )
    criteria = record.get("criteria", "")
    for criterion_type, criterion_text, criterion_order in split_eligibility_criteria(criteria):
        conn.execute(
            """
            INSERT INTO trial_eligibility_criteria
                (trial_uid, criterion_type, criterion_text, language, criterion_order, source_section)
            VALUES (?, ?, ?, 'en', ?, 'WHO ICTRP criteria')
            """,
            (trial_uid, criterion_type, criterion_text, criterion_order),
        )
    for country in record.get("countries", []):
        conn.execute(
            """
            INSERT OR REPLACE INTO trial_country_records
                (trial_uid, country, evidence_type, source_name, source_url, last_verified_at)
            VALUES (?, ?, 'registry_country_list', ?, ?, ?)
            """,
            (trial_uid, country, SOURCE_NAME, source_url, imported_at),
        )


def import_xml(
    conn: sqlite3.Connection,
    xml_path: Path,
    args: argparse.Namespace,
    search_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    imported_at = utc_now()
    terms_path = (Path.cwd() / args.terms).resolve() if not args.terms.is_absolute() else args.terms
    terms = load_term_config(terms_path)
    hard = TermMatcher(terms.get("hard_terms", []))
    context = TermMatcher(terms.get("context_terms", []))
    review = TermMatcher(terms.get("review_terms", []))
    search_id = conn.execute(
        """
        INSERT INTO who_search_runs
            (field_name, term, total_records, total_trials, searched_at, raw_result_summary, recruitment_status, query_json, xml_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            args.field,
            args.term,
            (search_meta or {}).get("total_records"),
            (search_meta or {}).get("total_trials"),
            imported_at,
            (search_meta or {}).get("summary", ""),
            args.recruitment_status,
            compact_json(search_terms(args)),
            str(xml_path),
        ),
    ).lastrowid
    counters = {"xml_records": 0, "loaded": 0, "not_recalled": 0, "duplicates": 0}
    seen: set[str] = set()
    for record in iter_xml_records(xml_path):
        counters["xml_records"] += 1
        trial_id = record["trial_id"]
        if trial_id in seen:
            counters["duplicates"] += 1
            continue
        seen.add(trial_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO who_download_hits
                (search_id, trial_id, field_name, term, xml_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (search_id, trial_id, args.field, args.term, str(xml_path)),
        )
        recall = recall_record(record, hard, context, review)
        conn.execute(
            """
            INSERT OR REPLACE INTO who_trial_records
                (trial_id, source_register, detail_fetched, search_hit_json, detail_json, source_url, imported_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (
                trial_id,
                record.get("register") or source_from_trial_id(trial_id),
                compact_json({"field": args.field, "term": args.term, "recruitment_status": args.recruitment_status, "query_terms": search_terms(args), "date_start": getattr(args, "date_start", ""), "date_end": getattr(args, "date_end", ""), "xml_path": str(xml_path)}),
                compact_json(record),
                record.get("source_url", ""),
                imported_at,
            ),
        )
        for hit_type, hits in [
            ("hard", recall["hard_hits"]),
            ("context", recall["context_hits"]),
            ("review", recall["review_hits"]),
        ]:
            for hit in hits:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO who_recall_hits
                        (trial_id, field_name, term, hit_type, confidence)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (trial_id, hit["field"], hit["term"], hit_type, recall["confidence"]),
                )
        if recall["confidence"] == "none":
            counters["not_recalled"] += 1
            continue
        insert_trial(conn, record, recall, imported_at)
        counters["loaded"] += 1
    conn.commit()
    return counters


def main() -> None:
    args = parse_args()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    search_meta: dict[str, Any] | None = None
    try:
        xml_path = args.xml.resolve() if args.xml else None
        if xml_path is None:
            xml_path, search_meta = download_xml(args)
        if args.download_only:
            report = {"xml_path": str(xml_path), "search": search_meta, "download_only": True}
            (REPORT_DIR / "who_build_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        conn = connect(args.out, args.reset)
        counters = import_xml(conn, xml_path, args, search_meta)
        matching_optimization = optimize_database(
            conn,
            coverage_scope={"source": "WHO ICTRP", "recruitment_status": [args.recruitment_status]},
        )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        report = {
            "db": str(args.out),
            "field": args.field,
            "term": args.term,
            "xml_path": str(xml_path),
            "search": search_meta,
            "counters": counters,
            "matching_optimization": matching_optimization,
        }
        (REPORT_DIR / "who_build_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("WHO XML export timed out. Retry later or use --xml with a manually downloaded file.") from exc


if __name__ == "__main__":
    main()















