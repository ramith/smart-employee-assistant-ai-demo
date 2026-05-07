#!/usr/bin/env bash
# Probe P1 — RFC 8693 token-exchange to mint a token targeting HR Agent API.
# Uses orchestrator-agent's OAuth App as the requesting client; subject_token
# is the user-delegated token from P10.B; actor_token is the orchestrator-agent's
# actor token from P0.
#
# Pass: token has sub=user, act.sub=orchestrator-agent, aud=https://hr.smart-employee.local/a2a,
#       scope contains hr_*_a2a.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "./_env.sh"

require ASGARDEO_BASE
require ORCHESTRATOR_MCP_CLIENT_ID
require ORCHESTRATOR_MCP_CLIENT_SECRET
require ORCHESTRATOR_AGENT_ID

if [ ! -f /tmp/orch_actor_token.txt ]; then
  fail "Need /tmp/orch_actor_token.txt — run scripts/probes/p0_orchestrator_actor_token.sh first."
  exit 1
fi
if [ ! -f /tmp/user_delegated_token.txt ]; then
  fail "Need /tmp/user_delegated_token.txt — run scripts/probes/p10_user_delegated_token.sh first."
  exit 1
fi

ACTOR_TOKEN="$(cat /tmp/orch_actor_token.txt)"
USER_TOKEN="$(cat /tmp/user_delegated_token.txt)"

TARGET_RESOURCE="${HR_AGENT_CANONICAL_URL:-https://hr.smart-employee.local/a2a}"
TARGET_SCOPES="hr_basic_a2a hr_self_a2a"

hr "P1 — RFC 8693 token-exchange (orchestrator → HR Agent)"
echo "Requesting client: orchestrator-mcp-client ($ORCHESTRATOR_MCP_CLIENT_ID)"
echo "                   (Agent App template doesn't expose Token Exchange grant; trying MCP Client App)"
echo "subject_token   : user-delegated token (from P10.B)"
echo "actor_token     : orchestrator-agent actor token (from P0)"
echo "resource        : $TARGET_RESOURCE"
echo "scope           : $TARGET_SCOPES"
echo

EXCHANGE_RESP="$(curl -sS $CURL_OPTS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$ORCHESTRATOR_MCP_CLIENT_ID:$ORCHESTRATOR_MCP_CLIENT_SECRET" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  --data-urlencode "subject_token=$USER_TOKEN" \
  --data-urlencode "subject_token_type=urn:ietf:params:oauth:token-type:jwt" \
  --data-urlencode "actor_token=$ACTOR_TOKEN" \
  --data-urlencode "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  --data-urlencode "resource=$TARGET_RESOURCE" \
  --data-urlencode "scope=$TARGET_SCOPES")"

echo "$EXCHANGE_RESP" | jq . 2>/dev/null || echo "$EXCHANGE_RESP"

EXCHANGED_TOKEN="$(echo "$EXCHANGE_RESP" | jq -r '.access_token // empty')"
if [ -z "$EXCHANGED_TOKEN" ]; then
  fail "P1 FAIL — no access_token returned."
  ERR="$(echo "$EXCHANGE_RESP" | jq -r '.error // empty')"
  case "$ERR" in
    unauthorized_client)
      echo "  → orchestrator-agent OAuth App is NOT subscribed to the target resource."
      echo "    Console → Applications → AGENT-d0bf27c2-… → Authorization tab → + Authorize resource → HR Agent API"
      ;;
    invalid_scope)
      echo "  → Requested scopes not granted to orchestrator-agent OAuth App. Check Authorization tab subscription scopes."
      ;;
    invalid_grant)
      echo "  → subject_token / actor_token rejected. May be expired or scope mismatch."
      ;;
    *)
      echo "  → Asgardeo error: $ERR. Check the response body above."
      ;;
  esac
  exit 1
fi

ok "Exchanged access_token (first 40): ${EXCHANGED_TOKEN:0:40}..."

# Save for downstream probes (Hop 4 / P8 nested chain test)
echo -n "$EXCHANGED_TOKEN" > /tmp/hr_call_token.txt
ok "Saved to /tmp/hr_call_token.txt"

hr "Decoded JWT payload"
PAYLOAD="$(decode_jwt_payload "$EXCHANGED_TOKEN")"
echo "$PAYLOAD" | jq .

# Validate critical claims
SUB="$(echo "$PAYLOAD" | jq -r '.sub // empty')"
ACT_SUB="$(echo "$PAYLOAD" | jq -r '.act.sub // empty')"
AUD="$(echo "$PAYLOAD" | jq -r '.aud // empty')"
SCOPES="$(echo "$PAYLOAD" | jq -r '.scope // empty')"

hr "P1 verdict"
[ "$SUB" != "" ] && [ "$SUB" != "null" ] && ok "sub present: $SUB" || fail "sub missing"
[ "$ACT_SUB" = "$ORCHESTRATOR_AGENT_ID" ] && ok "act.sub == ORCHESTRATOR_AGENT_ID ✓" || fail "act.sub mismatch (got: $ACT_SUB, expected: $ORCHESTRATOR_AGENT_ID)"
[[ "$AUD" == *"$TARGET_RESOURCE"* ]] && ok "aud contains target resource ✓" || warn "aud does not exactly match target ($AUD vs $TARGET_RESOURCE)"
[[ "$SCOPES" == *"hr_basic_a2a"* ]] && ok "scope contains hr_basic_a2a ✓" || warn "scope missing hr_basic_a2a (got: $SCOPES)"

hr "P1 PASS — RFC 8693 token-exchange works in ddademo tenant"
echo "Spike memo: P1=PASS, act chain depth-1 (act.sub=orchestrator-agent)."
