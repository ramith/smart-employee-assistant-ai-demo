# Sourced by every probe script. Loads ./.env per-service and exposes
# canonical variable names that the probe scripts use.
#
# Usage: source scripts/probes/_env.sh
#
# Targets WSO2 IS 7.2.0 on-prem at https://13.60.190.47:9443 (default).
# Asgardeo SaaS variables remain as legacy aliases during the transition.

set -e

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Pull from each service's .env (orchestrator first — has the bulk of values)
for svc in orchestrator hr-agent it-agent hr-server it-server; do
  if [ -f "$REPO/$svc/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$REPO/$svc/.env"
    set +a
  fi
done

# ─── Canonical IdP base ───────────────────────────────────────────────────
# Prefer WSO2_IS_* (current); fall back to ASGARDEO_* (legacy spike values).
export IDP_BASE="${WSO2_IS_BASE_URL:-${ASGARDEO_BASE_URL:-${ASGARDEO_BASE:-}}}"
export IDP_ISSUER="${WSO2_IS_ISSUER:-${ASGARDEO_ISSUER:-}}"
export IDP_JWKS_URL="${WSO2_IS_JWKS_URL:-${ASGARDEO_JWKS_URL:-}}"
export IDP_INTROSPECT_URL="${WSO2_IS_INTROSPECT_URL:-${ASGARDEO_INTROSPECT_URL:-}}"

# Backwards-compat: old probe scripts reference ASGARDEO_BASE.
export ASGARDEO_BASE="$IDP_BASE"
export ASGARDEO_ISSUER="$IDP_ISSUER"
export ASGARDEO_JWKS_URL="$IDP_JWKS_URL"
export ASGARDEO_INTROSPECT_URL="$IDP_INTROSPECT_URL"

# ─── TLS handling for WSO2 IS self-signed dev cert ────────────────────────
# WSO2 IS at 13.60.190.47:9443 ships with a self-signed cert. Set
# IDP_INSECURE_TLS=1 (default) to add `-k` to all curl probes.
# In production, set to 0 and ensure the cert is in the system trust store.
export IDP_INSECURE_TLS="${IDP_INSECURE_TLS:-1}"
if [ "$IDP_INSECURE_TLS" = "1" ]; then
  export CURL_OPTS="-k"
else
  export CURL_OPTS=""
fi

require() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "ERROR: $name not set. Edit \$REPO/<service>/.env or export it." >&2
    exit 1
  fi
}

# Pretty helpers
hr() { printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }
ok() { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# Decode JWT payload (no signature verification — diagnostic only).
decode_jwt_payload() {
  local jwt="$1"
  local payload
  payload="$(echo -n "$jwt" | cut -d. -f2)"
  local pad=$(( 4 - ${#payload} % 4 ))
  if [ $pad -ne 4 ]; then payload="${payload}$(printf '=%.0s' $(seq 1 $pad))"; fi
  echo "$payload" | tr '_-' '/+' | base64 -d 2>/dev/null
}
