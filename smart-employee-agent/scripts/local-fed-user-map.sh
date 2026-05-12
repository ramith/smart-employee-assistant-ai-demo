#!/usr/bin/env bash
# scripts/local-fed-user-map.sh — enable federated-to-local user mapping on an application
#                                 via the WSO2 IS Applications REST API.
#
# The WSO2 IS 7.x Console UI does not expose the "Use Mapped Local Subject" toggle
# for MCP Client Application template (known UI bug). This script PATCHes the
# claimConfiguration.subject.useMappedLocalSubject flag directly via the admin API.
#
# With this flag enabled, when a federated user (e.g. UAE Pass) logs in, IS resolves
# the locally JIT-provisioned account whose subject claim matches and uses that local
# identity as the token `sub` — instead of the raw federated identifier.
#
# Usage:
#   IS_ADMIN_USER=admin IS_ADMIN_PASS=admin APP_ID=85dbad28-068c-4a17-b308-07eb95d88076 \
#       ./scripts/local-fed-user-map.sh
#
# Required env:
#   IS_ADMIN_USER   IS Console admin username (e.g. "admin")
#   IS_ADMIN_PASS   IS Console admin password
#   APP_ID          Application resource ID (UUID) from IS Console > Applications
#
# Optional env:
#   IS_BASE         IS base URL   (default: https://13.60.190.47:9443)
#   INSECURE_TLS    "1" to curl -k (default: 1 for the dev instance)
#
# This script is idempotent — running it twice produces the same end state.

set -euo pipefail

IS_BASE="${IS_BASE:-https://13.60.190.47:9443}"
INSECURE_TLS="${INSECURE_TLS:-1}"

: "${IS_ADMIN_USER:?set IS_ADMIN_USER}"
: "${IS_ADMIN_PASS:?set IS_ADMIN_PASS}"
: "${APP_ID:?set APP_ID (application resource UUID from IS Console)}"

CURL_FLAGS=(-sS --fail-with-body)
[ "$INSECURE_TLS" = "1" ] && CURL_FLAGS+=(-k)

APP_URL="$IS_BASE/api/server/v1/applications/$APP_ID"

# 1. GET current claimConfiguration so we preserve existing claim mappings.
echo "→ fetching current claimConfiguration for app $APP_ID …" >&2
CURRENT=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$APP_URL")

# 2. Build the PATCH body: merge useMappedLocalSubject=true into the existing
#    subject block; clear role.claim.uri (IS rejects http://wso2.org/claims/role
#    on MCP app templates with APP-60001).
PATCH_BODY=$(printf '%s' "$CURRENT" | python3 -c '
import json, sys

doc = json.load(sys.stdin)
claim_cfg = doc.get("claimConfiguration", {})

# Flip the flag
subject = claim_cfg.setdefault("subject", {})
subject["useMappedLocalSubject"] = True

# IS rejects the default role claim URI on MCP app templates (APP-60001);
# clear it to an empty string to match what the Console sends.
role = claim_cfg.setdefault("role", {})
role.setdefault("claim", {})["uri"] = ""

print(json.dumps({"claimConfiguration": claim_cfg}))
')

# 3. PATCH the application.
echo "→ patching useMappedLocalSubject = true …" >&2
curl "${CURL_FLAGS[@]}" \
    -X PATCH \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    -H "Content-Type: application/json" \
    -d "$PATCH_BODY" \
    "$APP_URL" > /tmp/.local-fed-user-map.resp || {
    echo "PATCH failed. Response:" >&2
    cat /tmp/.local-fed-user-map.resp >&2
    rm -f /tmp/.local-fed-user-map.resp
    exit 1
}
rm -f /tmp/.local-fed-user-map.resp

# 4. Read back and verify.
echo "→ verifying …" >&2
GET_RESP=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$APP_URL")

ACTUAL=$(printf '%s' "$GET_RESP" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
print(doc["claimConfiguration"]["subject"]["useMappedLocalSubject"])
')

if [ "$ACTUAL" = "True" ]; then
    echo "✓ useMappedLocalSubject is now enabled on app $APP_ID"
else
    echo "✗ verification failed — useMappedLocalSubject=$ACTUAL" >&2
    exit 1
fi
