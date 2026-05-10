#!/usr/bin/env bash
# scripts/check-is-config.sh — Sprint 4 IS pre-flight audit.
#
# Audits the live WSO2 IS instance to confirm Sprint 4 prerequisites are
# in place before the implementation slices and Stage 11 manual test.
#
# Checks (in order):
#   1. IS connectivity   — JWKS endpoint returns HTTP 200.
#   2. JWKS payload      — JSON with a non-empty `keys` array.
#   3. Scopes registered — `hr_assets_write_rest` + `it_assets_self_rest`
#                          present as API resource scopes (Mgmt API).
#   4. Sample token      — client_credentials grant succeeds and the
#                          decoded access-token payload contains both
#                          `username` and `email` claims.
#
# Exits 0 on full PASS; 1 on any FAIL. WARN does not fail the script.
#
# Usage:
#   ./scripts/check-is-config.sh
#
# Environment variables (all optional, defaults shown):
#   IS_BASE_URL              https://13.60.190.47:9443
#   IS_ADMIN_USER            admin
#   IS_ADMIN_PASS            admin
#   IS_ADMIN_CLIENT_ID       (no default — script warns if unset)
#   IS_ADMIN_CLIENT_SECRET   (no default — script warns if unset)
#
# Exit codes:
#   0  — all checks PASS.
#   1  — at least one check FAIL, or jq missing, or arg error.

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
IS_BASE_URL="${IS_BASE_URL:-https://13.60.190.47:9443}"
IS_ADMIN_USER="${IS_ADMIN_USER:-admin}"
IS_ADMIN_PASS="${IS_ADMIN_PASS:-admin}"
IS_ADMIN_CLIENT_ID="${IS_ADMIN_CLIENT_ID:-}"
IS_ADMIN_CLIENT_SECRET="${IS_ADMIN_CLIENT_SECRET:-}"

REQUIRED_SCOPES=("hr_assets_write_rest" "it_assets_self_rest")
REQUIRED_CLAIMS=("username" "email")

# ── Colourised output (only if terminal supports >= 8 colours) ───────────────
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    _R="$(tput sgr0)"; _G="$(tput setaf 2)"; _Y="$(tput setaf 3)"; _RD="$(tput setaf 1)"; _B="$(tput bold)"
else
    _R=""; _G=""; _Y=""; _RD=""; _B=""
fi

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

pass() { printf "  ${_G}[PASS]${_R} %s\n" "$1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { printf "  ${_RD}[FAIL]${_R} %s — %s\n" "$1" "$2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
warn() { printf "  ${_Y}[WARN]${_R} %s — %s\n" "$1" "$2"; WARN_COUNT=$((WARN_COUNT + 1)); }
hdr()  { printf "\n${_B}── %s ──${_R}\n" "$1"; }

# ── Pre-flight: jq is required ───────────────────────────────────────────────
if ! command -v jq >/dev/null 2>&1; then
    printf "${_RD}error:${_R} jq not found on PATH. Install with: brew install jq\n" >&2
    exit 1
fi

# ── base64url decode (no padding, +/ → -_) ───────────────────────────────────
b64url_decode() {
    local s="$1"
    s="${s//-/+}"
    s="${s//_//}"
    case $(( ${#s} % 4 )) in
        2) s="${s}==" ;;
        3) s="${s}=" ;;
    esac
    printf '%s' "$s" | base64 -D 2>/dev/null || printf '%s' "$s" | base64 -d 2>/dev/null
}

# ── Check 1: connectivity to JWKS ────────────────────────────────────────────
check_connectivity() {
    local code
    code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 "${IS_BASE_URL}/oauth2/jwks" || echo "000")
    if [ "$code" = "200" ]; then
        pass "IS connectivity (${IS_BASE_URL}/oauth2/jwks → 200)"
    else
        fail "IS connectivity" "expected 200, got ${code}"
    fi
}

# ── Check 2: JWKS payload non-empty ──────────────────────────────────────────
check_jwks() {
    local body keys_len
    body=$(curl -sk --max-time 10 "${IS_BASE_URL}/oauth2/jwks" || echo "")
    if [ -z "$body" ]; then
        fail "JWKS payload" "empty response"
        return
    fi
    keys_len=$(printf '%s' "$body" | jq -r '.keys | length' 2>/dev/null || echo "0")
    if [ "$keys_len" -gt 0 ] 2>/dev/null; then
        pass "JWKS payload (keys[].length = ${keys_len})"
    else
        fail "JWKS payload" "no keys[] entries (parse failed or empty)"
    fi
}

# ── Check 3: required scopes registered as API resource scopes ───────────────
# Mgmt API endpoint may vary across IS versions; on FAIL we WARN (not hard
# fail) and tell the operator how to verify manually.
check_scopes_registered() {
    local resp http_code body
    resp=$(curl -sk -u "${IS_ADMIN_USER}:${IS_ADMIN_PASS}" \
                 -w "\n__HTTP_CODE__:%{http_code}" \
                 --max-time 15 \
                 "${IS_BASE_URL}/api/server/v1/api-resources/scopes" 2>/dev/null || echo "__HTTP_CODE__:000")
    http_code=$(printf '%s' "$resp" | sed -n 's/.*__HTTP_CODE__:\([0-9]*\).*/\1/p' | tail -n 1)
    body=$(printf '%s' "$resp" | sed 's/__HTTP_CODE__:[0-9]*$//')

    if [ "$http_code" != "200" ]; then
        warn "scope registration audit" "Mgmt API returned ${http_code}; verify manually in IS Console → API Resources → Scopes that '${REQUIRED_SCOPES[*]}' are registered"
        return
    fi

    local missing=()
    for scope in "${REQUIRED_SCOPES[@]}"; do
        if ! printf '%s' "$body" | jq -e --arg s "$scope" '.[]? | select(.name == $s)' >/dev/null 2>&1; then
            missing+=("$scope")
        fi
    done
    if [ ${#missing[@]} -eq 0 ]; then
        pass "API resource scopes registered (${REQUIRED_SCOPES[*]})"
    else
        fail "API resource scopes registered" "missing: ${missing[*]} — register in IS Console → API Resources → Scopes"
    fi
}

# ── Check 4: sample token claims (username, email) ───────────────────────────
check_sample_token_claims() {
    if [ -z "$IS_ADMIN_CLIENT_ID" ] || [ -z "$IS_ADMIN_CLIENT_SECRET" ]; then
        warn "sample token claims" "IS_ADMIN_CLIENT_ID / IS_ADMIN_CLIENT_SECRET unset — set them and re-run to audit token claims"
        return
    fi

    local resp access_token payload missing=()
    resp=$(curl -sk --max-time 15 \
                -u "${IS_ADMIN_CLIENT_ID}:${IS_ADMIN_CLIENT_SECRET}" \
                -d "grant_type=client_credentials" \
                "${IS_BASE_URL}/oauth2/token" 2>/dev/null || echo "")
    access_token=$(printf '%s' "$resp" | jq -r '.access_token // empty' 2>/dev/null)
    if [ -z "$access_token" ]; then
        fail "sample token issuance" "client_credentials grant did not return access_token (response: ${resp:0:200})"
        return
    fi

    # JWT structure: header.payload.signature — decode segment 2.
    local seg2
    seg2=$(printf '%s' "$access_token" | awk -F. '{print $2}')
    if [ -z "$seg2" ]; then
        fail "sample token format" "access_token is not a JWT (no payload segment)"
        return
    fi

    payload=$(b64url_decode "$seg2")
    if [ -z "$payload" ] || ! printf '%s' "$payload" | jq -e . >/dev/null 2>&1; then
        fail "sample token payload" "could not decode JWT payload as JSON"
        return
    fi

    for claim in "${REQUIRED_CLAIMS[@]}"; do
        if ! printf '%s' "$payload" | jq -e --arg c "$claim" 'has($c)' >/dev/null 2>&1; then
            missing+=("$claim")
        fi
    done
    if [ ${#missing[@]} -eq 0 ]; then
        pass "sample token claims (username, email present)"
    else
        fail "sample token claims" "missing: ${missing[*]} — map them into the access-token claim set in IS Console → API Resources → (your resource) → Scopes / OIDC Scopes, then ensure the OAuth app's scope list includes 'profile email' or the custom claim"
    fi
}

# ── Main dispatcher ──────────────────────────────────────────────────────────
hdr "Sprint 4 IS pre-flight (${IS_BASE_URL})"

check_connectivity
check_jwks
check_scopes_registered
check_sample_token_claims

hdr "Summary"
printf "  ${_G}PASS:${_R} %d  |  ${_RD}FAIL:${_R} %d  |  ${_Y}WARN:${_R} %d\n" \
       "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
