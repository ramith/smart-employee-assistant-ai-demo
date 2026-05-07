#!/usr/bin/env bash
# Probe P0 — Mint orchestrator-agent's actor_token via WSO2 IS's 3-step
# App-Native Authentication flow (`/oauth2/authorize` → `/oauth2/authn` →
# `/oauth2/token` with response_mode=direct).
#
# Targets WSO2 IS 7.2.0 on-prem at https://13.60.190.47:9443 (self-signed cert →
# CURL_OPTS=-k via _env.sh). Pattern is identical to what worked on Asgardeo
# SaaS during the spike; only the IdP base URL changes.
#
# Pass: prints an access_token and decoded payload. Token will be written
#       to /tmp/orch_actor_token.txt for use by P1.
# Fail: each step's response body is dumped so we can see why.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "./_env.sh"

require ASGARDEO_BASE
require ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID
require ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET
require ORCHESTRATOR_AGENT_ID
require ORCHESTRATOR_AGENT_SECRET

REDIRECT="${ORCHESTRATOR_AGENT_REDIRECT_URI:-http://localhost:5001/agent-callback}"

hr "P0 — Mint orchestrator-agent actor_token (3-step App-Native Auth)"
echo "Tenant base : $ASGARDEO_BASE"
echo "OAuth client: $ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID"
echo "Agent ID    : $ORCHESTRATOR_AGENT_ID"
echo "Redirect URI: $REDIRECT"
echo

# ─── 1) PKCE verifier + challenge ───────────────────────────────────────────
PKCE_VERIFIER="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' | head -c 64)"
PKCE_CHALLENGE="$(echo -n "$PKCE_VERIFIER" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')"

# ─── 2) /oauth2/authorize with response_mode=direct ─────────────────────────
# Basic-auth: OAuth App's client_id + OAuth App's client_secret (NOT the Agent Secret).
# The OAuth App and the Agent identity have separate secrets — see project memory.
hr "Step 2.1 — POST /oauth2/authorize (response_mode=direct)"
FLOW_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/authorize" \
  -u "$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID:$ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "client_id=$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID" \
  --data-urlencode "response_type=code" \
  --data-urlencode "redirect_uri=$REDIRECT" \
  --data-urlencode "scope=openid internal_login" \
  --data-urlencode "response_mode=direct" \
  --data-urlencode "code_challenge=$PKCE_CHALLENGE" \
  --data-urlencode "code_challenge_method=S256")"

echo "$FLOW_RESP" | jq . 2>/dev/null || echo "$FLOW_RESP"

FLOW_ID="$(echo "$FLOW_RESP" | jq -r '.flowId // empty')"
AUTHN_ID="$(echo "$FLOW_RESP" | jq -r '.nextStep.authenticators[0].authenticatorId // empty')"

if [ -z "$FLOW_ID" ] || [ -z "$AUTHN_ID" ]; then
  fail "No flowId or authenticatorId in response. Spike memo: P0 step 2.1 FAIL."
  echo "Common causes:"
  echo "  - ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID wrong"
  echo "  - App-Native Auth not enabled on the agent's OAuth App"
  echo "  - redirect_uri not registered exactly as '$REDIRECT'"
  echo "  - 'openid internal_login' scopes not granted"
  exit 1
fi
ok "flowId=$FLOW_ID  authenticator=$AUTHN_ID"

# ─── 3) /oauth2/authn with Agent ID + Secret ────────────────────────────────
hr "Step 2.2 — POST /oauth2/authn (Agent ID/Secret as username/password)"
AUTHN_BODY="$(jq -nc \
  --arg fid "$FLOW_ID" \
  --arg aid "$AUTHN_ID" \
  --arg u "$ORCHESTRATOR_AGENT_ID" \
  --arg p "$ORCHESTRATOR_AGENT_SECRET" \
  '{flowId:$fid, selectedAuthenticator:{authenticatorId:$aid, params:{username:$u, password:$p}}}')"

AUTHN_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/authn" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  -d "$AUTHN_BODY")"

echo "$AUTHN_RESP" | jq . 2>/dev/null || echo "$AUTHN_RESP"

CODE="$(echo "$AUTHN_RESP" | jq -r '.authData.code // .code // empty')"
if [ -z "$CODE" ]; then
  fail "No code in /authn response. Spike memo: P0 step 2.2 FAIL."
  echo "Common causes:"
  echo "  - Agent ID/Secret wrong"
  echo "  - Agent has been blocked (Console → Agents → Block toggle)"
  echo "  - Agent not assigned to a role with required scopes"
  exit 1
fi
ok "auth code=$CODE"

# ─── 4) /oauth2/token to get access token (Basic auth required) ─────────────
hr "Step 2.3 — POST /oauth2/token (auth code + PKCE verifier)"
TOKEN_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID:$ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "client_id=$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID" \
  --data-urlencode "code=$CODE" \
  --data-urlencode "code_verifier=$PKCE_VERIFIER" \
  --data-urlencode "redirect_uri=$REDIRECT")"

echo "$TOKEN_RESP" | jq . 2>/dev/null || echo "$TOKEN_RESP"

ACCESS_TOKEN="$(echo "$TOKEN_RESP" | jq -r '.access_token // empty')"
if [ -z "$ACCESS_TOKEN" ]; then
  fail "No access_token in /token response. Spike memo: P0 step 2.3 FAIL."
  exit 1
fi
ok "access_token (first 40 chars): ${ACCESS_TOKEN:0:40}..."

# Save for downstream probes (P1, P3, etc.).
echo -n "$ACCESS_TOKEN" > /tmp/orch_actor_token.txt
ok "Saved to /tmp/orch_actor_token.txt for P1+ probes."

# ─── 5) Decode and inspect ──────────────────────────────────────────────────
hr "Decoded JWT payload"
decode_jwt_payload "$ACCESS_TOKEN" | jq .

hr "P0 PASS"
echo "Capture into spike memo: probe P0, status=PASS, sub=<above>, iss=<above>, exp=<above>."
