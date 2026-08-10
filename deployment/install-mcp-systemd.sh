#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${WHO_DB_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_USER="${WHO_DB_SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${WHO_DB_SERVICE_GROUP:-$(id -gn)}"
PYTHON="${WHO_DB_PYTHON:-$PROJECT_ROOT/mcp_service/venv/bin/python}"
TEMPLATE="$PROJECT_ROOT/deployment/who-mcp.service.template"
ENV_FILE="/etc/who-mcp.env"

for path in "$PYTHON" "$TEMPLATE" "$PROJECT_ROOT/data/who_ictrp_cancer_trials.db"; do
  [[ -e "$path" ]] || { echo "Missing required path: $path" >&2; exit 2; }
done

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT
sed \
  -e "s|__PROJECT_ROOT__|$(escape_sed "$PROJECT_ROOT")|g" \
  -e "s|__SERVICE_USER__|$(escape_sed "$SERVICE_USER")|g" \
  -e "s|__SERVICE_GROUP__|$(escape_sed "$SERVICE_GROUP")|g" \
  -e "s|__PYTHON__|$(escape_sed "$PYTHON")|g" \
  "$TEMPLATE" > "$temporary"

if [[ ! -f "$ENV_FILE" ]]; then
  api_key="$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sudo install -m 0640 -o root -g "$SERVICE_GROUP" /dev/null "$ENV_FILE"
  {
    printf 'WHO_ICTRP_DB=%s/data/who_ictrp_cancer_trials.db\n' "$PROJECT_ROOT"
    printf 'WHO_MCP_TRANSPORT=streamable-http\n'
    printf 'WHO_MCP_API_KEY=%s\n' "$api_key"
    printf 'WHO_MCP_HOST=127.0.0.1\nWHO_MCP_PORT=18080\n'
  } | sudo tee "$ENV_FILE" >/dev/null
  echo "Created $ENV_FILE. Read the API key with: sudo sed -n 's/^WHO_MCP_API_KEY=//p' $ENV_FILE"
else
  echo "Keeping existing $ENV_FILE and API key."
fi

sudo install -m 0644 "$temporary" /etc/systemd/system/who-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now who-mcp.service
sudo systemctl status who-mcp.service --no-pager -l
