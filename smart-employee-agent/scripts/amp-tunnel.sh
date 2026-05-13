#!/usr/bin/env bash
# scripts/vm-tunnel.sh — SSH tunnel to the agent manager VM.
#
# Forward tunnels (local → VM):
#   3000  general service
#   8080  general service
#   8084  general service
#   9000  general service
#   9098  general service
#   22893 OTel collector
#
# Reverse tunnel (VM → local):
#   VM:19090 → localhost:8090  (lets Agent Manager on the VM reach the local orchestrator)
#
# Usage:
#   ./scripts/vm-tunnel.sh          # start in background
#   ./scripts/vm-tunnel.sh --fg     # start in foreground (Ctrl-C to stop)
#   ./scripts/vm-tunnel.sh stop     # stop background tunnel

set -euo pipefail
cd "$(dirname "$0")/.."

# ── Config ────────────────────────────────────────────────────────────────────
VM_HOST="${VM_HOST:-13.53.103.131}"
VM_USER="${VM_USER:-ubuntu}"
VM_KEY="${VM_KEY:-/Users/shammi/Projects/SARotation/AI-agent-id/dda-poc-key.pem}"

PIDFILE=".vm-tunnel.pid"
LOGFILE=".vm-tunnel.log"

_R="\033[0m"; _B="\033[1m"; _G="\033[32m"; _Y="\033[33m"; _RD="\033[31m"
ok()   { printf "  ${_G}✓${_R} %s\n" "$1"; }
info() { printf "  ${_B}→${_R} %s\n" "$1"; }
fail() { printf "  ${_RD}✗${_R} %s\n" "$1" >&2; }

# ── Stop subcommand ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "stop" ]]; then
  if [[ -f "$PIDFILE" ]]; then
    PID="$(cat "$PIDFILE" || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      echo "→ stopped PID $PID"
    fi
    rm -f "$PIDFILE"
  fi
  pkill -f "ssh.*${VM_HOST}" 2>/dev/null || true
  echo "✓ tunnel down"
  exit 0
fi

# ── Validate key ──────────────────────────────────────────────────────────────
if [[ ! -f "$VM_KEY" ]]; then
  fail "Key not found: $VM_KEY"
  fail "Set VM_KEY env var to the correct path."
  exit 1
fi
chmod 600 "$VM_KEY"

# ── Already running? ──────────────────────────────────────────────────────────
if [[ -f "$PIDFILE" ]]; then
  OLDPID="$(cat "$PIDFILE" || true)"
  if [[ -n "$OLDPID" ]] && kill -0 "$OLDPID" 2>/dev/null; then
    ok "tunnel already running (PID $OLDPID)"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

# ── SSH command ───────────────────────────────────────────────────────────────
SSH_CMD=(
  ssh
  -i "$VM_KEY"
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o ExitOnForwardFailure=yes
  -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes
  # Forward: local port → VM port
  -L 3000:localhost:3000
  -L 8080:localhost:8080
  -L 8084:localhost:8084
  -L 9000:localhost:9000
  -L 9098:localhost:9098
  -L 22893:localhost:22893
  # Reverse: VM port 19090 → local orchestrator on 8090
  -R 19090:localhost:8090
  -N
  "${VM_USER}@${VM_HOST}"
)

# ── Foreground mode ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "--fg" ]]; then
  info "Running in foreground — Ctrl-C to stop"
  exec "${SSH_CMD[@]}"
fi

# ── Background mode ───────────────────────────────────────────────────────────
info "Starting tunnel to ${VM_USER}@${VM_HOST} …"
"${SSH_CMD[@]}" >> "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
sleep 2

if ! kill -0 "$PID" 2>/dev/null; then
  fail "SSH exited immediately. Check $LOGFILE:"
  tail -5 "$LOGFILE" >&2
  rm -f "$PIDFILE"
  exit 1
fi

ok "tunnel up (PID $PID, logs: $LOGFILE)"
printf "\n  Forward tunnels (VM → local):\n"
printf "    localhost:3000   → VM:3000\n"
printf "    localhost:8080   → VM:8080\n"
printf "    localhost:8084   → VM:8084\n"
printf "    localhost:9000   → VM:9000\n"
printf "    localhost:9098   → VM:9098\n"
printf "    localhost:22893  → VM:22893  (OTel)\n"
printf "\n  Reverse tunnel (VM → local orchestrator):\n"
printf "    VM:19090         → localhost:8090\n"
printf "\n  Stop:  ./scripts/vm-tunnel.sh stop\n\n"
