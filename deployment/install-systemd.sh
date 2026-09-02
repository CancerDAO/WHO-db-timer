#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${WHO_DB_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_USER="${WHO_DB_SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${WHO_DB_SERVICE_GROUP:-$(id -gn)}"
PYTHON="${WHO_DB_PYTHON:-$PROJECT_ROOT/mcp_service/venv/bin/python}"
TEMPLATE="$PROJECT_ROOT/deployment/who-db-refresh.service.template"

for path in "$PYTHON" "$TEMPLATE" "$PROJECT_ROOT/deployment/who-db-refresh.timer" "$PROJECT_ROOT/scripts/verify_mcp_or_rollback.py"; do
  [[ -e "$path" ]] || { echo "Missing required path: $path" >&2; exit 2; }
done

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
root_escaped="$(escape_sed "$PROJECT_ROOT")"
user_escaped="$(escape_sed "$SERVICE_USER")"
group_escaped="$(escape_sed "$SERVICE_GROUP")"
python_escaped="$(escape_sed "$PYTHON")"

temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT
sed \
  -e "s|__PROJECT_ROOT__|$root_escaped|g" \
  -e "s|__SERVICE_USER__|$user_escaped|g" \
  -e "s|__SERVICE_GROUP__|$group_escaped|g" \
  -e "s|__PYTHON__|$python_escaped|g" \
  "$TEMPLATE" > "$temporary"

sudo install -m 0644 "$temporary" /etc/systemd/system/who-db-refresh.service
sudo install -m 0644 "$PROJECT_ROOT/deployment/who-db-refresh.timer" /etc/systemd/system/who-db-refresh.timer
sudo systemctl daemon-reload
sudo systemctl enable --now who-db-refresh.timer

echo "Installed for user $SERVICE_USER at $PROJECT_ROOT"
systemctl list-timers who-db-refresh.timer --all
