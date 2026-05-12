#!/usr/bin/env bash
#
# spike-bcl-up.sh — bring up the C12 BCL capture rig (Sprint 3 spike).
#
# Architecture:
#
#     AWS VM (running IS)                       laptop
#   ┌───────────────────────┐              ┌────────────────────────┐
#   │ WSO2 IS                │              │  bcl-listener          │
#   │   POST localhost:8123/bcl    ───────► │  (docker, bound to     │
#   │        ▲               │              │   127.0.0.1:8123)      │
#   │        │ reverse SSH   │              │                        │
#   │        ▼ tunnel        │  ◄── autossh ──── laptop initiates SSH
#   │ sshd (loopback bind)   │              │                        │
#   └───────────────────────┘              └────────────────────────┘
#
# Prereqs (one-time, run scripts/spike-bcl-prep-mac.sh):
#   * brew install autossh
#   * .env entries: AWS_VM_HOST, AWS_VM_USER
#   * SSH key already configured for the VM
#
# What this script does:
#   1. Validates .env + autossh installed.
#   2. docker compose --profile spike-bcl up -d bcl-listener
#   3. Waits for the listener to answer GET /healthz on 127.0.0.1:8123.
#   4. Starts autossh in the background with an aggressive keepalive.
#   5. Smoke-tests the tunnel by hitting localhost:8123 from the AWS VM.
#   6. Prints the BCL URL to register in WSO2 IS Console.
#
# Idempotent: re-running detects an existing autossh PID and skips relaunch.

set -euo pipefail

cd "$(dirname "$0")/.."

PIDFILE=".spike-bcl-autossh.pid"
TUNNEL_PORT=8123
LISTENER_HOST_PORT=8123

_R="\033[0m"; _B="\033[1m"; _G="\033[32m"; _Y="\033[33m"; _RD="\033[31m"
ok()   { printf "  ${_G}✓${_R} %s\n" "$1"; }
info() { printf "  ${_B}→${_R} %s\n" "$1"; }
warn() { printf "  ${_Y}!${_R} %s\n" "$1"; }
fail() { printf "  ${_RD}✗${_R} %s\n" "$1"; }
hdr()  { printf "\n${_B}── %s ──${_R}\n" "$1"; }

# ── Load .env ────────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  fail ".env missing. Run scripts/spike-bcl-prep-mac.sh first."
  exit 2
fi
set -a
# shellcheck disable=SC1091
. .env
set +a

if [[ -z "${AWS_VM_HOST:-}" || -z "${AWS_VM_USER:-}" ]]; then
  fail "AWS_VM_HOST / AWS_VM_USER not set in .env. Run spike-bcl-prep-mac.sh."
  exit 2
fi

if ! command -v autossh >/dev/null 2>&1; then
  fail "autossh not installed. Run scripts/spike-bcl-prep-mac.sh."
  exit 2
fi

# Optional explicit key path (~/path expansion handled).
SSH_KEY_OPT=()
if [[ -n "${AWS_VM_KEY:-}" ]]; then
  KEY_PATH="${AWS_VM_KEY/#~/$HOME}"
  if [[ ! -f "$KEY_PATH" ]]; then
    fail "AWS_VM_KEY points to missing file: $AWS_VM_KEY"
    exit 2
  fi
  SSH_KEY_OPT=(-i "$KEY_PATH")
fi

# ── 1. Bring up the bcl-listener container ───────────────────────────────────
hdr "Starting bcl-listener container"
docker compose --profile spike-bcl up -d bcl-listener
info "waiting for listener on 127.0.0.1:${LISTENER_HOST_PORT} …"
for i in {1..30}; do
  if curl -fsS "http://127.0.0.1:${LISTENER_HOST_PORT}/healthz" >/dev/null 2>&1; then
    ok "listener answering"
    break
  fi
  sleep 1
  if [[ $i -eq 30 ]]; then
    fail "listener never came up. Check: docker compose logs bcl-listener"
    exit 1
  fi
done

# ── 2. Stop any stale autossh ────────────────────────────────────────────────
if [[ -f "$PIDFILE" ]]; then
  OLDPID="$(cat "$PIDFILE" || true)"
  if [[ -n "$OLDPID" ]] && kill -0 "$OLDPID" 2>/dev/null; then
    warn "autossh PID $OLDPID already running — leaving it alone"
    AUTOSSH_PID="$OLDPID"
  else
    rm -f "$PIDFILE"
  fi
fi

# ── 3. Start autossh in the background ───────────────────────────────────────
if [[ -z "${AUTOSSH_PID:-}" ]]; then
  hdr "Starting reverse SSH tunnel"
  info "autossh -M 0 -N -R ${TUNNEL_PORT}:localhost:${LISTENER_HOST_PORT} ${AWS_VM_USER}@${AWS_VM_HOST}"
  AUTOSSH_LOGFILE=".spike-bcl-autossh.log"
  # Run autossh without -f: backgrounding via & lets the shell capture
  # the PID directly. autossh -M 0 -f causes an immediate crash-loop on
  # macOS because the -f daemonise path conflicts with ExitOnForwardFailure.
  AUTOSSH_GATETIME=0 \
  AUTOSSH_LOGFILE="$AUTOSSH_LOGFILE" \
  autossh -M 0 -N \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -o BatchMode=yes \
    "${SSH_KEY_OPT[@]}" \
    -R "${TUNNEL_PORT}:127.0.0.1:${LISTENER_HOST_PORT}" \
    "${AWS_VM_USER}@${AWS_VM_HOST}" \
    >> "$AUTOSSH_LOGFILE" 2>&1 &

  AUTOSSH_PID=$!
  if [[ -z "$AUTOSSH_PID" ]]; then
    fail "autossh did not start. See $AUTOSSH_LOGFILE."
    exit 1
  fi
  echo "$AUTOSSH_PID" > "$PIDFILE"
  ok "autossh PID $AUTOSSH_PID (logs: $AUTOSSH_LOGFILE)"
fi

# ── 4. Smoke-test the tunnel from the AWS VM ─────────────────────────────────
hdr "Tunnel smoke-test"
info "running curl localhost:${TUNNEL_PORT}/healthz on the AWS VM …"
sleep 1
if ssh -o BatchMode=yes -o ConnectTimeout=5 "${SSH_KEY_OPT[@]}" \
       "${AWS_VM_USER}@${AWS_VM_HOST}" \
       "curl -fsS http://127.0.0.1:${TUNNEL_PORT}/healthz" 2>/dev/null \
       | grep -q "BCL listener up"; then
  ok "AWS VM reaches laptop's bcl-listener via the tunnel"
else
  warn "smoke test did not return expected body — tunnel may be up but listener path mismatched"
  echo "        Try manually:  ssh ${AWS_VM_USER}@${AWS_VM_HOST} curl -v http://127.0.0.1:${TUNNEL_PORT}/healthz"
fi

# ── 5. Tell the operator what to do next ─────────────────────────────────────
cat <<EOF

  Spike rig is live.

  In WSO2 IS Console (https://${AWS_VM_HOST}:9443/console) for each agent
  application AND the orchestrator app:
      Application → Protocol → Logout URLs →
        Back channel logout URL: http://localhost:${TUNNEL_PORT}/bcl
      Click Update.

  Capture file (decoded JSON per POST):
      tools/_bcl_log/bcl_received.log

  Tail the listener live:
      docker compose --profile spike-bcl logs -f bcl-listener

  Tear down:
      ./scripts/spike-bcl-down.sh
EOF
