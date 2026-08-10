from __future__ import annotations

from pathlib import Path
from typing import Any

from query_service import database_metadata, search_trials_multidimensional


def execute_search_plan(
    db_path: Path,
    search_plan: dict[str, Any],
    *,
    country: str = "",
    max_per_query: int = 500,
    total_limit: int = 5000,
) -> dict[str, Any]:
    """Execute every keyword branch with pagination and explicit truncation audit."""
    max_per_query = max(1, min(int(max_per_query), 5000))
    total_limit = max(1, min(int(total_limit), 20000))
    page_size = min(100, max_per_query)
    merged: dict[str, dict[str, Any]] = {}
    query_audit: list[dict[str, Any]] = []
    for group in search_plan.get("keyword_groups") or []:
        label = str(group.get("label") or "unlabeled").strip()
        for query in group.get("queries") or []:
            condition = str(query.get("condition") or "").strip()
            term = str(query.get("term") or "").strip()
            if not condition and not term:
                continue
            offset = 0
            rows: list[dict[str, Any]] = []
            last_fts = ""
            probe_limit = max_per_query + 1
            while len(rows) < probe_limit:
                payload = search_trials_multidimensional(
                    db_path,
                    general_terms=[term] if term else None,
                    condition_terms=[condition] if condition else None,
                    country=country,
                    recruitment_statuses=["recruiting"],
                    interventional_only=True,
                    limit=min(page_size, probe_limit - len(rows)),
                    offset=offset,
                )
                page = payload["results"]
                last_fts = payload["query"]["fts"]
                rows.extend(page)
                if len(page) < min(page_size, probe_limit - len(rows) + len(page)):
                    break
                offset += len(page)
                if not page:
                    break
            truncated = len(rows) > max_per_query
            rows = rows[:max_per_query]
            query_audit.append({
                "label": label,
                "condition": condition,
                "term": term,
                "returned": len(rows),
                "pages": 0 if not rows else (len(rows) + page_size - 1) // page_size,
                "max_per_query": max_per_query,
                "truncated": truncated,
                "has_more": truncated,
                "complete": not truncated,
                "interventional_only": True,
                "fts": last_fts,
            })
            for result in rows:
                trial_uid = result["trial_uid"]
                if trial_uid not in merged:
                    result["matched_by"] = []
                    result["matched_queries"] = []
                    merged[trial_uid] = result
                if label not in merged[trial_uid]["matched_by"]:
                    merged[trial_uid]["matched_by"].append(label)
                merged[trial_uid]["matched_queries"].append({"condition": condition, "term": term})
    all_results = list(merged.values())
    results = all_results[:total_limit]
    return {
        "results": results,
        "search_stats": {
            "total_queries": len(query_audit),
            "unique_after_dedup": len(all_results),
            "returned": len(results),
            "total_limit": total_limit,
            "global_truncated": len(all_results) > len(results),
            "has_more": len(all_results) > len(results),
            "complete": len(all_results) <= len(results),
            "query_truncation_count": sum(bool(item["truncated"]) for item in query_audit),
        },
        "query_audit": query_audit,
        "database_as_of": database_metadata(db_path).get("database_as_of"),
    }
