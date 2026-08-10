from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

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

mcp = FastMCP("who-ictrp-cancer-trials", json_response=True, stateless_http=True)


@mcp.tool()
def database_metadata() -> dict[str, Any]:
    """Return the database build time, WHO search watermark, fetch time, schema, and index versions."""
    return query_database_metadata(DB_PATH)


@mcp.tool()
def database_summary() -> dict[str, Any]:
    """Return cached table counts, routing coverage, and the database update watermark."""
    return query_database_summary(DB_PATH)


@mcp.tool()
def search_trials(query: str, country: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Backward-compatible recruiting trial search using token-aware FTS across all matching fields."""
    payload = query_multidimensional(
        DB_PATH,
        general_terms=[query],
        country=country,
        recruitment_statuses=["recruiting"],
        limit=limit,
    )
    return payload["results"]


@mcp.tool()
def search_trials_multidimensional(
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
    """Search by independent clinical dimensions; dimensions are ANDed and alternatives within one dimension are ORed."""
    return query_multidimensional(
        DB_PATH,
        general_terms=general_terms,
        condition_terms=condition_terms,
        biomarker_terms=biomarker_terms,
        intervention_terms=intervention_terms,
        eligibility_terms=eligibility_terms,
        country=country,
        recruitment_statuses=recruitment_statuses,
        interventional_only=interventional_only,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def execute_search_plan(
    search_plan: dict[str, Any],
    country: str = "",
    max_per_query: int = 500,
    total_limit: int = 5000,
) -> dict[str, Any]:
    """Execute the clinical matching keyword_groups, union results, and deduplicate by canonical trial_uid."""
    return query_search_plan(
        DB_PATH,
        search_plan,
        country=country,
        max_per_query=max_per_query,
        total_limit=total_limit,
    )


@mcp.tool()
def get_trial(registry_id: str) -> dict[str, Any]:
    """Fetch one trial by primary ID, secondary ID, or trial_uid, including parsed eligibility and sites."""
    return query_get_trial(DB_PATH, registry_id)


@mcp.tool()
def country_stats(limit: int = 50) -> list[dict[str, Any]]:
    """Return trial counts by country/region for patient-relative routing."""
    return query_country_stats(DB_PATH, limit)


if __name__ == "__main__":
    transport = os.environ.get("WHO_MCP_TRANSPORT", "stdio").strip().casefold()
    if transport == "stdio":
        mcp.run()
    elif transport in {"http", "streamable-http"}:
        import uvicorn
        from http_auth import BearerTokenMiddleware

        api_key = os.environ.get("WHO_MCP_API_KEY", "")
        host = os.environ.get("WHO_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("WHO_MCP_PORT", "8000"))
        app = BearerTokenMiddleware(mcp.streamable_http_app(), api_key)
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        raise SystemExit("WHO_MCP_TRANSPORT must be stdio or streamable-http")
