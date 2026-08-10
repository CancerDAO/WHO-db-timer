#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${WHO_DB_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON="${WHO_DB_PYTHON:-$PROJECT_ROOT/mcp_service/venv/bin/python}"
SMOKE_DB="${WHO_DB_SMOKE_DB:-$PROJECT_ROOT/data/scheduled-smoke.db}"

cd "$PROJECT_ROOT"
rm -f -- "$SMOKE_DB" "$SMOKE_DB-wal" "$SMOKE_DB-shm"
xvfb-run -a "$PYTHON" scripts/run_full_who_pipeline.py \
  --reset --max-searches 1 --headful --out "$SMOKE_DB"
"$PYTHON" scripts/verify_production_db.py --db "$SMOKE_DB" --minimum-trials 1

echo "Smoke test passed. Temporary database: $SMOKE_DB"
