# Clinical Trial Matching Integration

The build pipeline materializes a matching layer after all WHO XML imports:

- `database_metadata`: stable build and source-search watermarks
- `database_table_stats`: cached counts for fast MCP summaries
- `trial_search_fts`: FTS5 index over title, summary, disease, intervention, and eligibility
- case-insensitive registry-ID and country/status indexes

## Build or migrate

Full builds call the optimizer automatically:

```bash
python scripts/run_full_who_pipeline.py --reset --headful
```

Upgrade an existing database without downloading WHO data again:

```bash
python scripts/matching_db.py \
  --db data/who_ictrp_cancer_trials.db \
  --strategy config/who_search_strategy.yaml
```

`source_searched_through` is the maximum `who_search_runs.searched_at` value. It is the
watermark to display as the local WHO snapshot time. It is deliberately separate from
individual trials' `last_update_date`, whose source formats are not uniform.

## MCP search contracts

`search_trials` remains backward compatible. It now uses token-aware FTS instead of a
full-table `%query%` scan.

`search_trials_multidimensional` supports independent dimensions:

- `condition_terms`
- `biomarker_terms`
- `intervention_terms`
- `eligibility_terms`
- `general_terms`
- `country`
- `recruitment_statuses`

Terms in one dimension are ORed. Active dimensions are ANDed. For example,
`condition_terms=["colorectal cancer", "solid tumor"]` and
`biomarker_terms=["KRAS G12C"]` means `(CRC OR solid tumor) AND KRAS G12C`.

`execute_search_plan` accepts the clinical matching project's existing `keyword_groups`.
Each `{condition, term}` query is executed as an intersection, results across queries are
unioned, and records are deduplicated by canonical `trial_uid`. `matched_by` and
`matched_queries` preserve retrieval provenance for downstream reports.

Eligibility returned by `get_trial` contains both raw `criteria` rows and the
`parsed_criteria` object expected by the matching project's `trial-gater`.
