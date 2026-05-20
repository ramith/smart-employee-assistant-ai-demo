#!/usr/bin/env bash
# scripts/set-subject-claim.sh — configure the OIDC subject claim (S5.12) on an
#                                application via the WSO2 IS Applications REST API.
#
# WHY THIS SCRIPT EXISTS
# ─────────────────────
# The code in both REST servers derives `username` and `email` from the token's
# `sub` claim when explicit profile claims are absent (see _AuthContext in
# it_server/rest_api/server.py and hr_server/rest_api/server.py). This derivation
# only works when `sub` is the user's email address — the S5.12 configuration.
#
# Without S5.12, `sub` is an opaque UUID, `username` and `email` are both `None`,
# and resource allocation lookups (cubicle / IT assets) silently return empty results
# even when assets have been assigned.
#
# This script sets `subject.claimUri = http://wso2.org/claims/emailaddress` on the
# target application so that IS uses the user's email as the OIDC subject, enabling
# the full `sub → username → asset` lookup chain.
#
# Usage:
#   IS_ADMIN_USER=admin IS_ADMIN_PASS=NewsMax@1234 \
#       APP_CLIENT_ID=<consumer-key> \
#       ./scripts/set-subject-claim.sh
#
# Required env:
#   IS_ADMIN_USER          IS Console admin username (e.g. "admin")
#   IS_ADMIN_PASS          IS Console admin password
#   APP_CLIENT_ID          Consumer key of the target application
#
# Optional env:
#   IS_BASE                IS base URL  (default: https://13.60.190.47:9443)
#   INSECURE_TLS           "1" to curl -k  (default: 1 for the dev RC)
#   SUBJECT_CLAIM_URI      Subject claim URI (default: http://wso2.org/claims/emailaddress)
#
# This script is idempotent — running it twice produces the same end state.
# Run once for each app that issues tokens consumed by the REST servers:
#   - orchestrator-mcp-client (token-A: SPA REST calls)
#   - hr-agent app            (token-C: CIBA OBO tokens)
#   - it-agent app            (token-C: CIBA OBO tokens)

set -euo pipefail

IS_BASE="${IS_BASE:-https://13.60.190.47:9443}"
INSECURE_TLS="${INSECURE_TLS:-1}"
SUBJECT_CLAIM_URI="${SUBJECT_CLAIM_URI:-http://wso2.org/claims/emailaddress}"

: "${IS_ADMIN_USER:?set IS_ADMIN_USER}"
: "${IS_ADMIN_PASS:?set IS_ADMIN_PASS}"
: "${APP_CLIENT_ID:?set APP_CLIENT_ID to the application consumer key}"

CURL_FLAGS=(-sS --fail-with-body)
[ "$INSECURE_TLS" = "1" ] && CURL_FLAGS+=(-k)

# 1. Resolve application resource ID by consumer key.
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

# 2. GET current OIDC config; merge subject.claimUri; PUT back.
echo "→ fetching current OIDC config…" >&2
CURRENT=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc")

echo "→ merging subject.claimUri = $SUBJECT_CLAIM_URI …" >&2
MERGED=$(printf '%s' "$CURRENT" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
claim_uri = sys.argv[1]

# Strip read-only / secret fields that break the PUT (same pattern as set-bcl-url.sh).
for field in ("state", "clientSecret"):
    doc.pop(field, None)

doc.setdefault("subject", {})["claimUri"] = claim_uri
print(json.dumps(doc))
' "$SUBJECT_CLAIM_URI")

curl "${CURL_FLAGS[@]}" \
    -X PUT \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    -H "Content-Type: application/json" \
    -d "$MERGED" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc" \
    > /tmp/.set-subject-claim.resp || {
    echo "PUT failed. Request body (merged):" >&2
    printf '%s\n' "$MERGED" >&2
    echo "Response body:" >&2
    cat /tmp/.set-subject-claim.resp >&2
    rm -f /tmp/.set-subject-claim.resp
    exit 1
}
rm -f /tmp/.set-subject-claim.resp

# 3. Read back to confirm.
echo "→ verifying via GET …" >&2
GET_RESP=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc")
ACTUAL=$(printf '%s' "$GET_RESP" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
subj = doc.get("subject") or {}
print(subj.get("claimUri") or "")
')

if [ "$ACTUAL" = "$SUBJECT_CLAIM_URI" ]; then
    echo "✓ subject.claimUri = $ACTUAL"
else
    echo "✗ subject.claimUri mismatch — wanted $SUBJECT_CLAIM_URI, got $ACTUAL" >&2
    echo "Full GET response:" >&2
    printf '%s\n' "$GET_RESP" >&2
    exit 1
fi
