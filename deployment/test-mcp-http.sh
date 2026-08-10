#!/usr/bin/env bash
set -euo pipefail

URL="${WHO_MCP_TEST_URL:-http://127.0.0.1:18080/mcp}"
API_KEY="${WHO_MCP_API_KEY:-}"
[[ -n "$API_KEY" ]] || { echo "Set WHO_MCP_API_KEY before running this test." >&2; exit 2; }

unauthorized_status="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$URL" \
  -H 'Content-Type: application/json' --data '{}')"
[[ "$unauthorized_status" == "401" ]] || {
  echo "Expected unauthenticated HTTP 401, got $unauthorized_status" >&2
  exit 1
}

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT
authorized_status="$(curl -sS -o "$response_file" -w '%{http_code}' -X POST "$URL" \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"deployment-smoke-test","version":"1.0"}}}')"
[[ "$authorized_status" == "200" ]] || {
  echo "Expected authenticated HTTP 200, got $authorized_status" >&2
  cat "$response_file" >&2
  exit 1
}
grep -q '"result"' "$response_file" || {
  echo "Initialize response does not contain a JSON-RPC result." >&2
  cat "$response_file" >&2
  exit 1
}

echo "MCP HTTP authentication and initialize smoke test passed."
