# WHO ICTRP Cancer Trials Database and MCP Server

Builds a searchable SQLite database from the WHO ICTRP Search Portal XML export, publishes validated refreshes atomically, and exposes read-only clinical-trial search tools through MCP.

## What is included

- WHO Search Portal automation with validated XML downloads and retry handling.
- A cancer-focused, recruiting-only search strategy split by date and field.
- Normalized trial, registry ID, intervention, eligibility, country-record and named-site tables.
- FTS5 multidimensional search and database watermark metadata.
- A read-only MCP server over stdio or API-key protected Streamable HTTP.
- A Linux systemd timer that builds staging, validates it, backs up production and atomically promotes it.

## Repository layout

```text
config/          WHO search strategy and cancer recall terms
schemas/         SQLite source schema
scripts/         download, build, index, validation and publication commands
mcp_service/     read-only MCP server and query layer
deployment/      generic systemd installer, timer and server smoke test
tests/           unit and contract tests
data/            runtime databases, XML and reports (not committed)
```

## Local setup

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r mcp_service/requirements.txt
python -m playwright install chromium
```

On Windows, activate with `.venv\Scripts\activate`.

## Build

Run one search as a non-production smoke test:

```bash
python scripts/run_full_who_pipeline.py \
  --reset --max-searches 1 --headful \
  --out data/scheduled-smoke.db
python scripts/verify_production_db.py \
  --db data/scheduled-smoke.db --minimum-trials 1
```

Run the complete configured WHO build:

```bash
python scripts/run_full_who_pipeline.py --reset --headful
python scripts/generate_who_quality_report.py
```

Reuse previously downloaded WHO XML files:

```bash
python scripts/run_full_who_pipeline.py --reset --reuse-existing-xml
```

The full output is `data/who_ictrp_cancer_trials.db`. Runtime databases, WHO XML, reports and logs are intentionally excluded from Git.

## Search strategy

The production strategy requests `Recruiting` trials and keeps title/condition searches separate from intervention searches. WHO field combinations are not treated as a reliable set union, so merging those fields into one query can reduce recall. The committed strategy is in `config/who_search_strategy.yaml`.

Country-level registry coverage is stored in `trial_country_records`. `trial_sites` is reserved for named facilities or city-level centers; a country record must not be presented as a confirmed site.

## Scheduled server refresh

The updater builds `*.next.db`, verifies integrity, foreign keys, required tables, FTS coverage, metadata and row-count continuity, then creates one rollback backup and atomically replaces production. XML exports not referenced by the active production database are removed after both successful and failed runs. After promotion, systemd restarts the loopback MCP service and performs an authenticated protocol smoke test; a failed smoke test atomically restores the backup and restarts MCP on the previous database. Failed builds leave the current production database untouched.

```bash
chmod +x deployment/*.sh
deployment/server-smoke-test.sh
deployment/install-systemd.sh
sudo systemctl start who-db-refresh.service
sudo journalctl -u who-db-refresh.service -f
```

The default schedule is Tuesday 23:30 Asia/Shanghai with up to 30 minutes randomized delay. See [SERVER_SCHEDULED_UPDATE_GUIDE_ZH.md](SERVER_SCHEDULED_UPDATE_GUIDE_ZH.md) for complete upload, deployment, verification, recovery and rollback steps.

## MCP server

The server exposes database metadata, multidimensional search-plan execution and trial detail retrieval. Configuration is environment-based; secrets are never stored in the repository.

```bash
export WHO_ICTRP_DB="$PWD/data/who_ictrp_cancer_trials.db"
export WHO_MCP_TRANSPORT=stdio
python mcp_service/server.py
```

For Streamable HTTP, set `WHO_MCP_TRANSPORT=streamable-http`, host, port and `WHO_MCP_API_KEY`. See [mcp_service/README.md](mcp_service/README.md) and [mcp_service/.env.example](mcp_service/.env.example).

On a systemd-based server, install the MCP service after a production database exists:

```bash
deployment/install-mcp-systemd.sh
export WHO_MCP_API_KEY="$(sudo sed -n 's/^WHO_MCP_API_KEY=//p' /etc/who-mcp.env)"
deployment/test-mcp-http.sh
unset WHO_MCP_API_KEY
```

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Tests do not perform a full WHO download. The explicit server smoke test is the network integration check.

## Safety and reproducibility

- Never commit `.env`, API keys, SSH credentials, databases, XML exports or virtual environments.
- Public HTTP deployment should be placed behind HTTPS and authentication.
- WHO source availability and portal behavior are external dependencies; inspect `scheduled_refresh_status.json` after every scheduled run.
- This database supports trial discovery and review. It is not a substitute for registry confirmation or clinical eligibility review.

## Additional documentation

- [Server deployment and scheduled updates (Chinese)](SERVER_SCHEDULED_UPDATE_GUIDE_ZH.md)
- [CancerDAO platform co-location and migration (Chinese)](PLATFORM_COLOCATION_GUIDE_ZH.md)
- [Matching integration contract](MATCHING_INTEGRATION.md)
- [MCP service usage](mcp_service/README.md)

## License

MIT. See [LICENSE](LICENSE).
