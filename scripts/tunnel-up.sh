#!/usr/bin/env bash
#
# scripts/tunnel-up.sh — open the reverse SSH tunnel for backchannel logout.
#
# The orchestrator container is bound to 127.0.0.1:8123 on the host.
# This script opens an autossh reverse tunnel so that WSO2 IS on the AWS VM
# can POST logout_tokens to http://localhost:8123/backchannel-logout, which
# routes through the tunnel to the orchestrator.
#
# Run AFTER demo-up.sh (the orchestrator must already hold port 8123).
#
# Usage:
#   ./scripts/tunnel-up.sh
#
# Tear down:
#   ./scripts/tunnel-down.sh
#
# Prerequisites (one-time):
#   ./scripts/spike-bcl-prep-mac.sh   (installs autossh, writes .env)
#
# BCL URL in IS (one-time, already configured):
#   http://localhost:8123/backchannel-logout

set -euo pipefail
cd "$(dirname "$0")/.."

PIDFILE=".spike-bcl-autossh.pid"
LOGFILE=".spike-bcl-autossh.log"
TUNNEL_PORT=8123

_R="\033[0m"; _B="\033[1m"; _G="\033[32m"; _Y="\033[33m"; _RD="\033[31m"
ok()   { printf "  ${_G}✓${_R} %s\n" "$1"; }
info() { printf "  ${_B}→${_R} %s\n" "$1"; }
fail() { printf "  ${_RD}✗${_R} %s\n" "$1"; }
hdr()  { printf "\n${_B}── %s ──${_R}\n" "$1"; }

# ── Load .env ────────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  fail ".env missing. Run scripts/spike-bcl-prep-mac.sh first."
  exit 2
fi
set -a; . .env; set +a

if [[ -z "${AWS_VM_HOST:-}" || -z "${AWS_VM_USER:-}" ]]; then
  fail "AWS_VM_HOST / AWS_VM_USER not set in .env. Run spike-bcl-prep-mac.sh."
  exit 2
fi

if ! command -v autossh >/dev/null 2>&1; then
  fail "autossh not installed. Run scripts/spike-bcl-prep-mac.sh."
  exit 2
fi

# ── Check the orchestrator is already holding port 8123 ──────────────────────
hdr "Checking orchestrator is up on port $TUNNEL_PORT"
if ! curl -fsS "http://127.0.0.1:${TUNNEL_PORT}/healthz" >/dev/null 2>&1; then
  fail "Nothing answering on 127.0.0.1:${TUNNEL_PORT}. Run demo-up.sh first."
  exit 1
fi
ok "orchestrator healthy on port $TUNNEL_PORT"

# ── SSH key ───────────────────────────────────────────────────────────────────
SSH_KEY_OPT=()
if [[ -n "${AWS_VM_KEY:-}" ]]; then
  KEY_PATH="${AWS_VM_KEY/#~/$HOME}"
  if [[ ! -f "$KEY_PATH" ]]; then
    fail "AWS_VM_KEY points to missing file: $AWS_VM_KEY"
    exit 2
  fi
  SSH_KEY_OPT=(-i "$KEY_PATH")
fi

# ── Reuse existing tunnel if still alive ─────────────────────────────────────
if [[ -f "$PIDFILE" ]]; then
  OLDPID="$(cat "$PIDFILE" || true)"
  if [[ -n "$OLDPID" ]] && kill -0 "$OLDPID" 2>/dev/null; then
    ok "autossh PID $OLDPID already running — nothing to do"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

# ── Start autossh ─────────────────────────────────────────────────────────────
hdr "Starting reverse SSH tunnel (VM:$TUNNEL_PORT → orchestrator:$TUNNEL_PORT)"
AUTOSSH_GATETIME=0 \
AUTOSSH_LOGFILE="$LOGFILE" \
autossh -M 0 -N \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=accept-new \
  -o BatchMode=yes \
  "${SSH_KEY_OPT[@]}" \
  -R "${TUNNEL_PORT}:127.0.0.1:${TUNNEL_PORT}" \
  "${AWS_VM_USER}@${AWS_VM_HOST}" \
  >> "$LOGFILE" 2>&1 &

AUTOSSH_PID=$!
echo "$AUTOSSH_PID" > "$PIDFILE"
sleep 3

if ! kill -0 "$AUTOSSH_PID" 2>/dev/null; then
  fail "autossh exited immediately. See $LOGFILE."
  exit 1
fi
ok "autossh PID $AUTOSSH_PID (logs: $LOGFILE)"

# ── Smoke-test from the VM ────────────────────────────────────────────────────
hdr "Tunnel smoke-test"
info "curling http://127.0.0.1:${TUNNEL_PORT}/healthz from the AWS VM …"
if ssh -o BatchMode=yes -o ConnectTimeout=5 "${SSH_KEY_OPT[@]}" \
       "${AWS_VM_USER}@${AWS_VM_HOST}" \
       "curl -fsS --max-time 5 http://127.0.0.1:${TUNNEL_PORT}/healthz" 2>/dev/null \
       | grep -q "ok"; then
  ok "IS can reach the orchestrator via the tunnel"
else
  printf "  ${_Y}!${_R} smoke-test inconclusive — tunnel may still be stabilising\n"
  printf "  Retry manually:  ssh %s@%s curl -fsS http://127.0.0.1:%s/healthz\n" \
    "$AWS_VM_USER" "$AWS_VM_HOST" "$TUNNEL_PORT"
fi

cat <<EOF

  Tunnel is live.

  IS will POST logout_tokens to:
      http://localhost:${TUNNEL_PORT}/backchannel-logout  (→ orchestrator BCL receiver)

  To trigger a BCL event:
      IS Console → User Management → Active Sessions → terminate a session

  Watch orchestrator BCL logs:
      docker compose logs -f orchestrator | grep -i bcl

  Tear down:
      ./scripts/tunnel-down.sh
EOF
