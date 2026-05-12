#!/usr/bin/env bash
# scripts/add-callback-url.sh — register a callback/redirect URI on orchestrator-mcp-client
#                               via the WSO2 IS Applications REST API.
#
# Usage:
#   IS_ADMIN_USER=admin IS_ADMIN_PASS=NewsMax@1234 \
#       ./scripts/add-callback-url.sh
#
# Required env:
#   IS_ADMIN_USER              IS Console admin username
#   IS_ADMIN_PASS              IS Console admin password
#   ORCHESTRATOR_MCP_CLIENT_ID Consumer key of orchestrator-mcp-client (from orchestrator/.env)
#
# Optional env:
#   IS_BASE        IS base URL           (default: https://13.60.190.47:9443)
#   CALLBACK_URL   Redirect URI to add   (default: $ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI or http://localhost:8090/agent-callback)
#   APP_CLIENT_ID  Override consumer key (default: $ORCHESTRATOR_MCP_CLIENT_ID)
#   INSECURE_TLS   "1" to curl -k        (default: 1)
#
# Idempotent — already-registered URIs are not duplicated.

set -euo pipefail

IS_BASE="${IS_BASE:-https://13.60.190.47:9443}"
APP_CLIENT_ID="${APP_CLIENT_ID:-${ORCHESTRATOR_MCP_CLIENT_ID:-}}"
CALLBACK_URL="${CALLBACK_URL:-${ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI:-http://localhost:8090/agent-callback}}"
INSECURE_TLS="${INSECURE_TLS:-1}"

: "${IS_ADMIN_USER:?set IS_ADMIN_USER}"
: "${IS_ADMIN_PASS:?set IS_ADMIN_PASS}"
: "${APP_CLIENT_ID:?set ORCHESTRATOR_MCP_CLIENT_ID (or APP_CLIENT_ID) to the orchestrator-mcp-client consumer key}"

CURL_FLAGS=(-sS --fail-with-body)
[ "$INSECURE_TLS" = "1" ] && CURL_FLAGS+=(-k)

echo "→ resolving application id for consumer key prefix=${APP_CLIENT_ID:0:8}…" >&2
APP_LIST=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$IS_BASE/api/server/v1/applications?filter=clientId+eq+$APP_CLIENT_ID")
APP_ID=$(printf '%s' "$APP_LIST" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
apps = doc.get("applications", [])
if not apps:
    sys.exit("no application matched the consumer key")
print(apps[0]["id"])
')
echo "→ application id: $APP_ID" >&2

echo "→ fetching current OIDC config…" >&2
CURRENT=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc")

echo "→ merging callback URL: $CALLBACK_URL …" >&2
MERGED=$(printf '%s' "$CURRENT" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
new_url = sys.argv[1]

for field in ("state", "clientSecret"):
    doc.pop(field, None)

cbs = doc.get("callbackURLs") or []
if new_url not in cbs:
    cbs.append(new_url)
    doc["callbackURLs"] = cbs
    print(json.dumps(doc), file=sys.stderr)
else:
    print(f"  (already present — no change needed)", file=sys.stderr)
print(json.dumps(doc))
' "$CALLBACK_URL")

curl "${CURL_FLAGS[@]}" \
    -X PUT \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    -H "Content-Type: application/json" \
    -d "$MERGED" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc" \
    > /tmp/.add-callback-url.resp || {
    echo "PUT failed. Response:" >&2
    cat /tmp/.add-callback-url.resp >&2
    rm -f /tmp/.add-callback-url.resp
    exit 1
}
rm -f /tmp/.add-callback-url.resp

# Verify
echo "→ verifying via GET…" >&2
GET_RESP=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc")
REGISTERED=$(printf '%s' "$GET_RESP" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for u in (doc.get("callbackURLs") or []):
    print(u)
')

if printf '%s\n' "$REGISTERED" | grep -qF "$CALLBACK_URL"; then
    echo "✓ callback URL registered: $CALLBACK_URL"
    echo "  all registered URLs:"
    printf '%s\n' "$REGISTERED" | sed 's/^/    /'
else
    echo "✗ verification failed — $CALLBACK_URL not found in registered URLs" >&2
    printf '%s\n' "$REGISTERED" >&2
    exit 1
fi
