#!/usr/bin/env bash
# scripts/set-bcl-url.sh — set back_channel_logout_uri on orchestrator-mcp-client
#                         via the WSO2 IS Applications REST API.
#
# The new WSO2 IS 7.x Console UI does not expose this field for the
# "MCP Client Application" template, but the property is fully supported
# at the schema + runtime layer (see memory: project_orchestrator_app_vestigial.md).
# This script PATCHes it via the Applications admin API.
#
# Usage:
#   IS_ADMIN_USER=admin IS_ADMIN_PASS=admin BCL_URL=http://localhost:8123/backchannel-logout \\
#       ./scripts/set-bcl-url.sh
#
# Required env:
#   IS_ADMIN_USER   IS Console admin username (e.g. "admin")
#   IS_ADMIN_PASS   IS Console admin password
#   BCL_URL         The URL IS should POST logout_token to (your tunnel target)
#
# Optional env:
#   IS_BASE         IS base URL                (default: https://13.60.190.47:9443)
#   APP_CLIENT_ID   Target consumer key        (default: orchestrator-mcp-client)
#   INSECURE_TLS    "1" to curl -k             (default: 1 for the dev RC)
#
# This script is idempotent — running it twice produces the same end state.

set -euo pipefail

IS_BASE="${IS_BASE:-https://13.60.190.47:9443}"
APP_CLIENT_ID="${APP_CLIENT_ID:-Ry9Wx_Q7w2FSi27miUpYr3O0xR4a}"  # orchestrator-mcp-client
INSECURE_TLS="${INSECURE_TLS:-1}"

: "${IS_ADMIN_USER:?set IS_ADMIN_USER}"
: "${IS_ADMIN_PASS:?set IS_ADMIN_PASS}"
: "${BCL_URL:?set BCL_URL (the target URL IS should POST to)}"

CURL_FLAGS=(-sS --fail-with-body)
[ "$INSECURE_TLS" = "1" ] && CURL_FLAGS+=(-k)

# 1. Look up the application's resource ID by consumer key. The
#    Applications API filters with SCIM-style ?filter=clientId+eq+...
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

# 2. PATCH the OIDC inbound protocol with backChannelLogoutUrl. The
#    Applications API exposes it as inboundProtocolConfiguration.oidc.logout.backChannelLogoutUrl.
echo "→ PATCHing back_channel_logout_uri = $BCL_URL …" >&2
PATCH_BODY=$(python3 -c '
import json, sys
print(json.dumps({
    "logout": {
        "backChannelLogoutUrl": sys.argv[1]
    }
}))
' "$BCL_URL")

curl "${CURL_FLAGS[@]}" \
    -X PUT \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    -H "Content-Type: application/json" \
    -d "$PATCH_BODY" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc" \
    > /tmp/.set-bcl-url.resp || {
    echo "PATCH failed. Response body:" >&2
    cat /tmp/.set-bcl-url.resp >&2
    rm -f /tmp/.set-bcl-url.resp
    exit 1
}
rm -f /tmp/.set-bcl-url.resp

# 3. Read back to confirm.
echo "→ verifying via GET …" >&2
GET_RESP=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc")
ACTUAL=$(printf '%s' "$GET_RESP" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
logout = doc.get("logout") or {}
print(logout.get("backChannelLogoutUrl") or "")
')

if [ "$ACTUAL" = "$BCL_URL" ]; then
    echo "✓ back_channel_logout_uri now set to $ACTUAL"
else
    echo "✗ verification mismatch — wanted $BCL_URL, got $ACTUAL" >&2
    echo "Full GET response:" >&2
    printf '%s\n' "$GET_RESP" >&2
    exit 1
fi
