#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${WHO_DB_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

required=(
  "$PROJECT_ROOT/scripts/run_full_who_pipeline.py"
  "$PROJECT_ROOT/scripts/refresh_production_who_db.py"
  "$PROJECT_ROOT/config/who_search_strategy.yaml"
  "$PROJECT_ROOT/schemas/registry_schema.sql"
  "$PROJECT_ROOT/deployment/who-db-refresh.service.template"
  "$PROJECT_ROOT/deployment/who-db-refresh.timer"
  "$PROJECT_ROOT/deployment/who-db-refresh-requirements.txt"
  "$PROJECT_ROOT/deployment/install-systemd.sh"
)
for path in "${required[@]}"; do
  [[ -f "$path" ]] || { echo "Required deployment file is missing: $path" >&2; exit 3; }
done

echo "Stopping only the database refresh timer/service; the public who-mcp service stays online."
sudo systemctl stop who-db-refresh.timer || true
sudo systemctl stop who-db-refresh.service || true

echo "Removing known smoke-test and failed-staging artifacts from $PROJECT_ROOT."
rm -f -- \
  "$PROJECT_ROOT/data/scheduled-smoke.db" \
  "$PROJECT_ROOT/data/scheduled-smoke.db-wal" \
  "$PROJECT_ROOT/data/scheduled-smoke.db-shm" \
  "$PROJECT_ROOT/data/who_ictrp_cancer_trials.next.db" \
  "$PROJECT_ROOT/data/who_ictrp_cancer_trials.next.db-wal" \
  "$PROJECT_ROOT/data/who_ictrp_cancer_trials.next.db-shm" \
  "$PROJECT_ROOT/data/who_ictrp_cancer_trials.who-next.db" \
  "$PROJECT_ROOT/data/who_ictrp_cancer_trials.who-next.db-wal" \
  "$PROJECT_ROOT/data/who_ictrp_cancer_trials.who-next.db-shm"

echo "Installing refresh dependencies and Playwright Chromium."
"$PROJECT_ROOT/mcp_service/venv/bin/pip" install \
  -r "$PROJECT_ROOT/deployment/who-db-refresh-requirements.txt"
"$PROJECT_ROOT/mcp_service/venv/bin/python" -m playwright install chromium

sudo systemctl reset-failed who-db-refresh.service || true
"$PROJECT_ROOT/deployment/install-systemd.sh"

echo
echo "Redeployment complete. The production database and public MCP service were not removed."
systemctl list-timers who-db-refresh.timer --all
