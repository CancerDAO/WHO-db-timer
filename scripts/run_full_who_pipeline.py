from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from crawl_and_build_who_db import RAW_XML_DIR, connect, download_xml, import_xml, safe_slug, validate_xml_export
from matching_db import optimize_database
from who_common import DEFAULT_DB, REPORT_DIR, utc_now

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the WHO ICTRP XML export strategy and build one deduplicated SQLite DB.")
    parser.add_argument("--strategy", type=Path, default=PROJECT_ROOT / "config" / "who_search_strategy.yaml")
    parser.add_argument("--terms", type=Path, default=PROJECT_ROOT / "config" / "cancer_recall_terms.yaml")
    parser.add_argument("--out", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Skip search terms already committed to the output DB.")
    parser.add_argument("--include-optional", action="store_true")
    parser.add_argument("--max-searches", type=int, default=0, help="Smoke-test limit; 0 runs all selected searches.")
    parser.add_argument("--reuse-existing-xml", action="store_true", help="Use the newest matching XML in data/raw_xml before downloading again.")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--chrome-exe", default="")
    parser.add_argument("--search-timeout-ms", type=int, default=180000)
    parser.add_argument("--download-timeout-ms", type=int, default=1800000)
    parser.add_argument("--download-retries", type=int, default=3)
    parser.add_argument("--search-delay-seconds", type=float, default=10.0)
    return parser.parse_args()


def load_strategy(path: Path, include_optional: bool) -> list[dict[str, Any]]:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    searches = list(config.get("searches") or [])
    if include_optional:
        searches.extend(config.get("optional_searches") or [])
    normalized = []
    seen = set()
    for search in searches:
        field = str(search.get("field", "")).strip().lower()
        term = str(search.get("term", "")).strip()
        if field not in {"title", "condition", "intervention", "title_condition"} or not term:
            continue
        title_term = str(search.get("title_term", "")).strip()
        condition_term = str(search.get("condition_term", "")).strip()
        intervention_term = str(search.get("intervention_term", "")).strip()
        date_start = str(search.get("date_start", "")).strip()
        date_end = str(search.get("date_end", "")).strip()
        recruitment_status = str(search.get("recruitment_status", "recruiting")).strip().lower()
        if recruitment_status not in {"recruiting", "all"}:
            recruitment_status = "recruiting"
        key = (field, term.lower(), title_term.lower(), condition_term.lower(), intervention_term.lower(), date_start, date_end, recruitment_status)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "field": field,
            "term": term,
            "title_term": title_term,
            "condition_term": condition_term,
            "intervention_term": intervention_term,
            "date_start": date_start,
            "date_end": date_end,
            "recruitment_status": recruitment_status,
        })
    return normalized


def latest_matching_xml(field: str, term: str, recruitment_status: str) -> Path | None:
    prefix = f"who_ictrp_{recruitment_status}_{field}_{safe_slug(term)}_"
    candidates = sorted(RAW_XML_DIR.glob(prefix + "*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if validate_xml_export(candidate)["valid"]:
            return candidate
    return None


def db_counts(conn) -> dict[str, int]:
    tables = [
        "who_search_runs",
        "who_download_hits",
        "who_trial_records",
        "who_recall_hits",
        "raw_trial_records",
        "trial_master",
        "trial_registry_ids",
        "trial_interventions",
        "trial_eligibility_criteria",
        "trial_sites",
    ]
    out = {}
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in tables:
        if table in existing:
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return out


def main() -> None:
    args = parse_args()
    searches = load_strategy(args.strategy, args.include_optional)
    if args.max_searches:
        searches = searches[: args.max_searches]
    if not searches:
        raise SystemExit("No valid searches configured.")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_XML_DIR.mkdir(parents=True, exist_ok=True)
    conn = None if args.download_only else connect(args.out, args.reset)
    all_searches = list(searches)
    report_path = REPORT_DIR / "who_full_pipeline_report.json"
    started_at = utc_now()
    runs = []
    failures = []
    if args.resume:
        if conn is None:
            raise SystemExit("--resume cannot be combined with --download-only.")
        completed = {
            (str(row[0]), str(row[1]), str(row[2] or ""))
            for row in conn.execute("SELECT field_name, term, recruitment_status FROM who_search_runs")
        }
        searches = [
            search for search in searches
            if (search["field"], search["term"], search["recruitment_status"]) not in completed
        ]
        if report_path.exists():
            prior = json.loads(report_path.read_text(encoding="utf-8"))
            try:
                same_output = Path(prior.get("out", "")).resolve() == args.out.resolve()
            except (OSError, TypeError):
                same_output = False
            if same_output:
                started_at = prior.get("started_at") or started_at
                runs = list(prior.get("runs") or [])
                failures = list(prior.get("failures") or [])
        print(json.dumps({"resume": True, "completed": len(completed), "pending": len(searches)}, ensure_ascii=False), flush=True)
    report = {
        "started_at": started_at,
        "updated_at": utc_now(),
        "strategy": str(args.strategy),
        "out": str(args.out),
        "download_only": args.download_only,
        "include_optional": args.include_optional,
        "runs": runs,
        "failures": failures,
    }
    try:
        for position, search in enumerate(searches, start=1):
            index = len(runs) + 1
            run_args = SimpleNamespace(
                out=args.out,
                reset=False,
                terms=args.terms,
                field=search["field"],
                term=search["term"],
                title_term=search.get("title_term", ""),
                condition_term=search.get("condition_term", ""),
                intervention_term=search.get("intervention_term", ""),
                date_start=search.get("date_start", ""),
                date_end=search.get("date_end", ""),
                recruitment_status=search.get("recruitment_status", "recruiting"),
                xml=None,
                download_only=args.download_only,
                headful=args.headful,
                chrome_exe=args.chrome_exe,
                search_timeout_ms=args.search_timeout_ms,
                download_timeout_ms=args.download_timeout_ms,
            )
            xml_path = latest_matching_xml(run_args.field, run_args.term, run_args.recruitment_status) if args.reuse_existing_xml else None
            search_meta = None
            action = "reuse_xml" if xml_path else "download_xml"
            if xml_path is None:
                attempt_errors = []
                for attempt in range(1, max(args.download_retries, 1) + 1):
                    try:
                        xml_path, search_meta = download_xml(run_args)
                        if attempt_errors:
                            search_meta["attempt_errors"] = attempt_errors
                        break
                    except Exception as exc:
                        attempt_errors.append({"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"})
                        if attempt >= max(args.download_retries, 1):
                            failure = {
                                "field": run_args.field,
                                "term": run_args.term,
                                "recruitment_status": run_args.recruitment_status,
                                "query_terms": {
                                    "title": run_args.title_term,
                                    "condition": run_args.condition_term,
                                    "intervention": run_args.intervention_term,
                                    "date_start": run_args.date_start,
                                    "date_end": run_args.date_end,
                                },
                                "failed_at": utc_now(),
                                "attempt_errors": attempt_errors,
                            }
                            failures.append(failure)
                            report.update({
                                "updated_at": failure["failed_at"],
                                "failures": failures,
                                "last_error": failure,
                            })
                            report_path.write_text(
                                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8",
                            )
                            raise
                        print(json.dumps({"term": run_args.term, "retry": attempt + 1, "error": str(exc)}, ensure_ascii=False), flush=True)
                        time.sleep(min(5 * attempt, 30))
            item = {
                "index": index,
                "field": run_args.field,
                "term": run_args.term,
                "recruitment_status": run_args.recruitment_status,
                "query_terms": {
                    "title": run_args.title_term,
                    "condition": run_args.condition_term,
                    "intervention": run_args.intervention_term,
                    "date_start": run_args.date_start,
                    "date_end": run_args.date_end,
                },
                "action": action,
                "xml_path": str(xml_path),
                "search": search_meta,
            }
            if conn is not None:
                item["import_counters"] = import_xml(conn, xml_path, run_args, search_meta)
            runs.append(item)
            report = {
                "started_at": started_at,
                "updated_at": utc_now(),
                "strategy": str(args.strategy),
                "out": str(args.out),
                "download_only": args.download_only,
                "include_optional": args.include_optional,
                "runs": runs,
                "failures": failures,
            }
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(item, ensure_ascii=False, sort_keys=True), flush=True)
            if position < len(searches) and args.search_delay_seconds > 0:
                time.sleep(args.search_delay_seconds)
        if conn is not None:
            conn.commit()
            finished_at = utc_now()
            report["finished_at"] = finished_at
            report["matching_optimization"] = optimize_database(
                conn,
                built_at=finished_at,
                strategy_path=args.strategy,
                coverage_scope={
                    "source": "WHO ICTRP",
                    "recruitment_status": sorted({search["recruitment_status"] for search in all_searches}),
                    "searches": len(all_searches),
                },
            )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            report["final_counts"] = db_counts(conn)
    finally:
        if conn is not None:
            conn.close()
    report.setdefault("finished_at", utc_now())
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"runs": len(runs), "out": str(args.out), "final_counts": report.get("final_counts", {})}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()





