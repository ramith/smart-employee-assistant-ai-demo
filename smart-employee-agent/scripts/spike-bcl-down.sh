#!/usr/bin/env bash
#
# spike-bcl-down.sh — stop the C12 BCL capture rig.
#
# 1. Kills the autossh process recorded in .spike-bcl-autossh.pid.
# 2. docker compose --profile spike-bcl down (stops bcl-listener).
# 3. Leaves tools/_bcl_log/bcl_received.log in place.
#
# Idempotent.

set -euo pipefail

cd "$(dirname "$0")/.."

PIDFILE=".spike-bcl-autossh.pid"

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "→ stopping autossh PID $PID"
    kill "$PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null || true
    fi
  fi
  rm -f "$PIDFILE"
fi

# Belt-and-braces: if a stray autossh is still around for our VM, kill it.
if [[ -f .env ]]; then
  set -a; . .env; set +a
fi
if [[ -n "${AWS_VM_HOST:-}" ]]; then
  pkill -f "autossh.*${AWS_VM_HOST}" 2>/dev/null || true
fi

# Stop + remove ONLY the bcl-listener service. ``docker compose down`` (even
# with ``--profile``) tears down the shared network and any running services,
# which would kill the regular demo stack — so we use ``rm -fsv`` for a
# targeted stop+remove of just this one container.
echo "→ docker compose rm -fsv bcl-listener"
docker compose rm -fsv bcl-listener 2>/dev/null || true

echo "✓ rig torn down. Captures preserved at tools/_bcl_log/bcl_received.log"
echo "  (demo stack — orchestrator/agents/servers — left untouched)"
