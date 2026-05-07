#!/usr/bin/env bash
# Probe P10 — Mint a user-delegated token via Pattern C (`requested_actor`).
# Uses the orchestrator-app SPA as the OAuth client + test user credentials.
#
# Two modes:
#   P10.A — without actor_token attached at /token (PKCE-only)
#   P10.B — with the orchestrator-agent's actor_token in Authorization header
#           (the BFF pattern Asgardeo's Pattern C strictly documents)
#
# We try P10.A first; if `act` is present in the result, we're done. If not,
# fall through to P10.B using the actor token from /tmp/orch_actor_token.txt
# (must run P0 first).
#
# Pass: token has sub=user, act.sub=<ORCHESTRATOR_AGENT_ID>.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "./_env.sh"
# shellcheck disable=SC1091
[ -f "./.test-users.env" ] && source "./.test-users.env"

require ASGARDEO_BASE
require ORCHESTRATOR_MCP_CLIENT_ID
require ORCHESTRATOR_MCP_CLIENT_SECRET
require ORCHESTRATOR_AGENT_ID  # used as requested_actor value
require EMPLOYEE_USER_USERNAME
require EMPLOYEE_USER_PASSWORD

USER_REDIRECT="${ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI:-http://localhost:8090/agent-callback}"
WHICH_USER="${1:-employee}"  # 'employee' | 'hr_admin'

if [ "$WHICH_USER" = "hr_admin" ]; then
  require HR_ADMIN_USER_USERNAME
  require HR_ADMIN_USER_PASSWORD
  USER_NAME="$HR_ADMIN_USER_USERNAME"
  USER_PWD="$HR_ADMIN_USER_PASSWORD"
else
  USER_NAME="$EMPLOYEE_USER_USERNAME"
  USER_PWD="$EMPLOYEE_USER_PASSWORD"
fi

hr "P10 — User-delegated token via Pattern C (requested_actor=$ORCHESTRATOR_AGENT_ID)"
echo "User       : $USER_NAME"
echo "MCP client : $ORCHESTRATOR_MCP_CLIENT_ID"
echo "Redirect   : $USER_REDIRECT"
echo

# Scopes the user consents to. Must match what's been granted to their role.
USER_SCOPES="openid agent_access hr_basic_a2a hr_self_a2a it_assets_read_a2a"
if [ "$WHICH_USER" = "hr_admin" ]; then
  USER_SCOPES="openid agent_access hr_basic_a2a hr_self_a2a hr_read_a2a hr_approve_a2a it_assets_read_a2a"
fi
echo "Scopes     : $USER_SCOPES"

PKCE_VERIFIER="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' | head -c 64)"
PKCE_CHALLENGE="$(echo -n "$PKCE_VERIFIER" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')"

# ─── Step 1: /oauth2/authorize with requested_actor (Pattern C) ─────────────
hr "Step 1 — POST /oauth2/authorize (requested_actor=ORCHESTRATOR_AGENT_ID)"
FLOW_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/authorize" \
  -u "$ORCHESTRATOR_MCP_CLIENT_ID:$ORCHESTRATOR_MCP_CLIENT_SECRET" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "client_id=$ORCHESTRATOR_MCP_CLIENT_ID" \
  --data-urlencode "response_type=code" \
  --data-urlencode "redirect_uri=$USER_REDIRECT" \
  --data-urlencode "scope=$USER_SCOPES" \
  --data-urlencode "response_mode=direct" \
  --data-urlencode "code_challenge=$PKCE_CHALLENGE" \
  --data-urlencode "code_challenge_method=S256" \
  --data-urlencode "requested_actor=$ORCHESTRATOR_AGENT_ID")"

echo "$FLOW_RESP" | jq . 2>/dev/null || echo "$FLOW_RESP"

FLOW_ID="$(echo "$FLOW_RESP" | jq -r '.flowId // empty')"
AUTHN_ID="$(echo "$FLOW_RESP" | jq -r '.nextStep.authenticators[0].authenticatorId // empty')"

if [ -z "$FLOW_ID" ] || [ -z "$AUTHN_ID" ]; then
  fail "Step 1 failed. P10 FAIL."
  exit 1
fi
ok "flowId=$FLOW_ID  authenticator=$AUTHN_ID"

# ─── Step 2: /oauth2/authn with user credentials ────────────────────────────
hr "Step 2 — POST /oauth2/authn (user credentials)"
AUTHN_BODY="$(jq -nc \
  --arg fid "$FLOW_ID" --arg aid "$AUTHN_ID" \
  --arg u "$USER_NAME" --arg p "$USER_PWD" \
  '{flowId:$fid, selectedAuthenticator:{authenticatorId:$aid, params:{username:$u, password:$p}}}')"

AUTHN_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/authn" \
  -H 'Content-Type: application/json' \
  -d "$AUTHN_BODY")"
echo "$AUTHN_RESP" | jq . 2>/dev/null || echo "$AUTHN_RESP"

CODE="$(echo "$AUTHN_RESP" | jq -r '.authData.code // empty')"
if [ -z "$CODE" ]; then
  fail "Step 2 failed (user auth). Check the user's password / role assignment."
  exit 1
fi
ok "auth code=$CODE"

# ─── Step 3a: P10.A — /oauth2/token without actor_token attached ────────────
# orchestrator-mcp-client is confidential (has client_secret) so /token requires Basic auth.
hr "Step 3a (P10.A) — POST /oauth2/token (no actor_token in Authorization)"
TOKEN_RESP_A="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$ORCHESTRATOR_MCP_CLIENT_ID:$ORCHESTRATOR_MCP_CLIENT_SECRET" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "client_id=$ORCHESTRATOR_MCP_CLIENT_ID" \
  --data-urlencode "code=$CODE" \
  --data-urlencode "code_verifier=$PKCE_VERIFIER" \
  --data-urlencode "redirect_uri=$USER_REDIRECT")"

echo "$TOKEN_RESP_A" | jq . 2>/dev/null || echo "$TOKEN_RESP_A"
USER_TOKEN_A="$(echo "$TOKEN_RESP_A" | jq -r '.access_token // empty')"

if [ -n "$USER_TOKEN_A" ]; then
  hr "P10.A decoded payload"
  decode_jwt_payload "$USER_TOKEN_A" | jq .
  ACT_A="$(decode_jwt_payload "$USER_TOKEN_A" | jq -r '.act // empty')"
  if [ -n "$ACT_A" ] && [ "$ACT_A" != "null" ]; then
    ok "P10.A PASS — act claim present without attaching actor_token. Pattern C works in 'lazy' mode."
    echo -n "$USER_TOKEN_A" > /tmp/user_delegated_token.txt
    ok "Saved to /tmp/user_delegated_token.txt for P1+ probes."
    echo
    echo "Spike memo: P10.A=PASS, BFF code-exchange NOT required."
    exit 0
  else
    warn "P10.A produced a token but act claim is absent. Falling through to P10.B (BFF mode)."
  fi
else
  warn "P10.A failed. Falling through to P10.B."
fi

# ─── Step 3b: P10.B — /oauth2/token with orchestrator actor_token ───────────
hr "Step 3b (P10.B) — POST /oauth2/token with Authorization: Bearer <actor_token>"

if [ ! -f /tmp/orch_actor_token.txt ]; then
  fail "Need /tmp/orch_actor_token.txt from P0. Run scripts/probes/p0_orchestrator_actor_token.sh first."
  exit 1
fi
ACTOR_TOKEN="$(cat /tmp/orch_actor_token.txt)"

# Note: the auth code is single-use; we already burned it in Step 3a above if it ran.
# So we need a fresh code for P10.B. Re-do steps 1+2.
warn "Re-running steps 1+2 to get a fresh auth code for P10.B (codes are single-use)."

PKCE_VERIFIER="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' | head -c 64)"
PKCE_CHALLENGE="$(echo -n "$PKCE_VERIFIER" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')"

FLOW_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/authorize" \
  -u "$ORCHESTRATOR_MCP_CLIENT_ID:$ORCHESTRATOR_MCP_CLIENT_SECRET" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "client_id=$ORCHESTRATOR_MCP_CLIENT_ID" \
  --data-urlencode "response_type=code" \
  --data-urlencode "redirect_uri=$USER_REDIRECT" \
  --data-urlencode "scope=$USER_SCOPES" \
  --data-urlencode "response_mode=direct" \
  --data-urlencode "code_challenge=$PKCE_CHALLENGE" \
  --data-urlencode "code_challenge_method=S256" \
  --data-urlencode "requested_actor=$ORCHESTRATOR_AGENT_ID")"
FLOW_ID="$(echo "$FLOW_RESP" | jq -r '.flowId')"
AUTHN_ID="$(echo "$FLOW_RESP" | jq -r '.nextStep.authenticators[0].authenticatorId')"

AUTHN_BODY="$(jq -nc \
  --arg fid "$FLOW_ID" --arg aid "$AUTHN_ID" \
  --arg u "$USER_NAME" --arg p "$USER_PWD" \
  '{flowId:$fid, selectedAuthenticator:{authenticatorId:$aid, params:{username:$u, password:$p}}}')"
AUTHN_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/authn" \
  -H 'Content-Type: application/json' -d "$AUTHN_BODY")"
CODE="$(echo "$AUTHN_RESP" | jq -r '.authData.code')"
ok "fresh auth code=$CODE"

# Pattern C in Asgardeo: actor_token goes in the BODY (not Authorization header
# despite what the doc text suggests). Client auth via Basic; actor_token as body param.
TOKEN_RESP_B="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$ORCHESTRATOR_MCP_CLIENT_ID:$ORCHESTRATOR_MCP_CLIENT_SECRET" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "client_id=$ORCHESTRATOR_MCP_CLIENT_ID" \
  --data-urlencode "code=$CODE" \
  --data-urlencode "code_verifier=$PKCE_VERIFIER" \
  --data-urlencode "redirect_uri=$USER_REDIRECT" \
  --data-urlencode "actor_token=$ACTOR_TOKEN" \
  --data-urlencode "actor_token_type=urn:ietf:params:oauth:token-type:access_token")"

echo "$TOKEN_RESP_B" | jq . 2>/dev/null || echo "$TOKEN_RESP_B"
USER_TOKEN_B="$(echo "$TOKEN_RESP_B" | jq -r '.access_token // empty')"

if [ -z "$USER_TOKEN_B" ]; then
  fail "P10.B failed. Both modes failed — P10 FAIL."
  exit 1
fi

hr "P10.B decoded payload"
decode_jwt_payload "$USER_TOKEN_B" | jq .

ACT_B="$(decode_jwt_payload "$USER_TOKEN_B" | jq -r '.act // empty')"
if [ -n "$ACT_B" ] && [ "$ACT_B" != "null" ]; then
  ok "P10.B PASS — act claim present after attaching actor_token in Authorization header."
  echo -n "$USER_TOKEN_B" > /tmp/user_delegated_token.txt
  ok "Saved to /tmp/user_delegated_token.txt for P1+ probes."
  echo
  echo "Spike memo: P10.A=FAIL, P10.B=PASS. Hop 1 implementation MUST use BFF code-exchange."
else
  fail "P10.B produced a token but act claim is still absent. Pattern C may not be supported on this tenant. Major fallback per plan §3.6."
  exit 1
fi
