#!/usr/bin/env bash
# scripts/set-bcl-url.sh — set logout-related URLs on orchestrator-mcp-client
#                         via the WSO2 IS Applications REST API.
#
# WHY THIS SCRIPT EXISTS
# ─────────────────────
# The WSO2 IS 7.x Console UI does not expose two logout-related fields for the
# "MCP Client Application" template:
#
#   1. backChannelLogoutUrl  — the URL IS POSTs logout_token to (BCL / RFC 7009).
#      Field: logout.backChannelLogoutUrl in the OIDC inbound config.
#
#   2. post_logout_redirect_uri — IS validates this against callbackURLs at
#      RP-initiated logout.  When the app redirects to /oidc/logout with
#      post_logout_redirect_uri=http://localhost:8090/ and that URI is not in
#      callbackURLs, IS returns:
#        oauthErrorCode=access_denied
#        oauthErrorMsg=Post+logout+URI+does+not+match+with+registered+callback+URI
#      The GUI "Authorized redirect URLs" field only shows the auth-code callback;
#      there is no separate "Post Logout Redirect URI" field in the MCP template.
#
# Both are fully supported at the schema + runtime layer; the gap is purely in
# the Console UI template.  This script merges both in a single GET → merge → PUT.
#
# Usage:
#   IS_ADMIN_USER=admin IS_ADMIN_PASS=admin BCL_URL=http://localhost:8123/backchannel-logout \
#       ./scripts/set-bcl-url.sh
#
# Required env:
#   IS_ADMIN_USER          IS Console admin username (e.g. "admin")
#   IS_ADMIN_PASS          IS Console admin password
#   BCL_URL                The URL IS should POST logout_token to (your tunnel target)
#
# Optional env:
#   POST_LOGOUT_URI        URI to add to callbackURLs for RP-initiated logout redirect
#                          (default: http://localhost:8090/)
#   IS_BASE                IS base URL  (default: https://13.60.190.47:9443)
#   APP_CLIENT_ID          Target consumer key  (default: orchestrator-mcp-client)
#   INSECURE_TLS           "1" to curl -k  (default: 1 for the dev RC)
#
# This script is idempotent — running it twice produces the same end state.

set -euo pipefail

IS_BASE="${IS_BASE:-https://13.60.190.47:9443}"
APP_CLIENT_ID="${APP_CLIENT_ID:-8SDRXOI_4zOrNBgV4KUUfDPs3Tsa}"  # orchestrator-mcp-client
INSECURE_TLS="${INSECURE_TLS:-1}"
POST_LOGOUT_URI="${POST_LOGOUT_URI:-http://localhost:8090/}"

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

# 2. GET the current OIDC config; merge in our logout field; PUT back
#    the FULL config. WSO2 IS's PUT on inbound-protocols/oidc is a
#    full-replacement (not a patch); a partial body fails validation
#    with size-constraint errors on the empty required arrays.
echo "→ fetching current OIDC config to merge…" >&2
CURRENT=$(curl "${CURL_FLAGS[@]}" \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc")

echo "→ merging back_channel_logout_uri = $BCL_URL, post_logout_redirect_uri = $POST_LOGOUT_URI …" >&2
MERGED=$(printf '%s' "$CURRENT" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
bcl_url = sys.argv[1]
post_logout_uri = sys.argv[2]

# Per application.yaml schema:
#   - "state" is readOnly — sending it back triggers 400.
#   - "clientSecret" view requires the
#     internal_application_mgt_client_secret_view scope; without it the
#     GET returns a placeholder. Echoing that placeholder on PUT can
#     corrupt the live secret. Safer to omit; IS leaves the existing
#     secret untouched when the field is absent.
for field in ("state", "clientSecret"):
    doc.pop(field, None)

# 1. Back-channel logout URL.
doc.setdefault("logout", {})["backChannelLogoutUrl"] = bcl_url

# 2. post_logout_redirect_uri — IS validates this against callbackURLs at
#    RP-initiated logout. The MCP Client Application template has no GUI
#    field for it; we register it here.
#    IS does NOT support multiple plain entries in callbackURLs ("Multiple
#    callbacks for OAuth2 are not supported yet. Please use regex to define
#    multiple callbacks."). So we build a single regexp= pattern that covers
#    both the auth-code callback and the post-logout redirect.
#    Pattern: regexp=http://localhost:8090/(agent-callback)?
#    This matches:  http://localhost:8090/agent-callback  and  http://localhost:8090/
existing = doc.get("callbackURLs") or []
# Extract plain URIs from any existing regexp= entries so re-runs stay idempotent.
# A regexp= entry stores an alternation:  regexp=(uri1|uri2|...)
# We un-escape each branch back to a plain URI (re.escape is the only transform applied).
import re as _re
plain_callbacks = []
for entry in existing:
    if entry.startswith("regexp="):
        pattern = entry[len("regexp="):]
        # Strip outer parens if present: (branch1|branch2)
        pattern = pattern.strip()
        if pattern.startswith("(") and pattern.endswith(")"):
            pattern = pattern[1:-1]
        for branch in pattern.split("|"):
            # Reverse re.escape: unescape \x -> x for the characters we know escape
            plain_callbacks.append(_re.sub(r"\\(.)", r"\1", branch))
    else:
        plain_callbacks.append(entry)
all_uris = list(dict.fromkeys(plain_callbacks + [post_logout_uri]))  # dedup, order-preserving
if len(all_uris) == 1:
    doc["callbackURLs"] = all_uris
else:
    # Build a regex alternation; escape dots for URI safety
    import re
    parts = "|".join(re.escape(u) for u in all_uris)
    doc["callbackURLs"] = [f"regexp=({parts})"]

print(json.dumps(doc))
' "$BCL_URL" "$POST_LOGOUT_URI")

curl "${CURL_FLAGS[@]}" \
    -X PUT \
    -u "$IS_ADMIN_USER:$IS_ADMIN_PASS" \
    -H "Content-Type: application/json" \
    -d "$MERGED" \
    "$IS_BASE/api/server/v1/applications/$APP_ID/inbound-protocols/oidc" \
    > /tmp/.set-bcl-url.resp || {
    echo "PUT failed. Request body (merged):" >&2
    printf '%s\n' "$MERGED" >&2
    echo "Response body:" >&2
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
ACTUAL_BCL=$(printf '%s' "$GET_RESP" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
logout = doc.get("logout") or {}
print(logout.get("backChannelLogoutUrl") or "")
')
ACTUAL_CALLBACKS=$(printf '%s' "$GET_RESP" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
print(json.dumps(doc.get("callbackURLs") or []))
')

OK=1
if [ "$ACTUAL_BCL" = "$BCL_URL" ]; then
    echo "✓ backChannelLogoutUrl = $ACTUAL_BCL"
else
    echo "✗ backChannelLogoutUrl mismatch — wanted $BCL_URL, got $ACTUAL_BCL" >&2
    OK=0
fi

if printf '%s' "$ACTUAL_CALLBACKS" | python3 -c "
import json, sys, re
uris = json.load(sys.stdin)
target = sys.argv[1]
for entry in uris:
    if entry.startswith('regexp='):
        pattern = entry[len('regexp='):]
        if re.fullmatch(pattern, target):
            sys.exit(0)
    elif entry == target:
        sys.exit(0)
sys.exit(1)
" "$POST_LOGOUT_URI"; then
    echo "✓ callbackURLs covers post_logout_redirect_uri $POST_LOGOUT_URI"
else
    echo "✗ callbackURLs does not contain $POST_LOGOUT_URI  (got $ACTUAL_CALLBACKS)" >&2
    OK=0
fi

[ "$OK" = "1" ] || { echo "Full GET response:" >&2; printf '%s\n' "$GET_RESP" >&2; exit 1; }
