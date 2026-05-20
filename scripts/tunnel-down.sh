#!/usr/bin/env bash
#
# scripts/tunnel-down.sh — stop the reverse SSH tunnel for backchannel logout.
#
# Counterpart to scripts/tunnel-up.sh. Leaves the demo stack untouched.

set -euo pipefail
cd "$(dirname "$0")/.."

PIDFILE=".spike-bcl-autossh.pid"

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "→ stopping autossh PID $PID"
    kill "$PID" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi

# Belt-and-braces: kill any stray autossh for this VM.
if [[ -f .env ]]; then
  set -a; . .env; set +a
fi
if [[ -n "${AWS_VM_HOST:-}" ]]; then
  pkill -f "autossh.*${AWS_VM_HOST}" 2>/dev/null || true
fi

echo "✓ tunnel down"
