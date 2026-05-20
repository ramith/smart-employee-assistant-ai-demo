#!/usr/bin/env bash
#
# grep-trace.sh — Reconstruct a single user request across all services
# by greppping docker compose logs for a given X-Request-ID.
#
# Usage:
#     ./scripts/grep-trace.sh <request-id>
#     ./scripts/grep-trace.sh d8f3a2c1-4b7e-4a8d-9c1f-2a5b6c7d8e9f
#
# Output:
#     One log line per hop, prefixed with the service name. Lines are sorted
#     by timestamp so the chain reads top-to-bottom in causal order.
#
# Demo flow this reconstructs (UC-03):
#     SPA → orchestrator (chat) → A2A → hr_agent → MCP → hr_server
#                              → A2A → it_agent → MCP → it_server
#
# Exit codes:
#     0 — at least one match found
#     1 — no match (wrong rid? logs already rotated?)
#     2 — usage error
#
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <request-id>" >&2
  echo "  hint: $ docker compose logs --tail=2000 orchestrator | grep chat_request" >&2
  exit 2
fi

RID="$1"

# Validate rid shape: UUID4 (8-4-4-4-12 hex). Prevents shell-meta injection
# and stops accidental matches if the caller pastes a chat message that
# happens to contain the rid as a substring.
if ! [[ "$RID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
  echo "error: '$RID' does not look like a UUID" >&2
  exit 2
fi

SERVICES=(orchestrator hr_agent it_agent hr_server it_server)

# Collect log lines from each service that match the rid, prefix them with
# the service name, then sort by timestamp (the second field).
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

for svc in "${SERVICES[@]}"; do
  # docker compose logs --no-color --no-log-prefix prints raw service stdout/stderr.
  docker compose logs --no-color --no-log-prefix "$svc" 2>/dev/null \
    | grep -F " ${RID} " \
    | sed "s/^/${svc} | /" \
    >> "$TMP" || true
done

if [[ ! -s "$TMP" ]]; then
  echo "no log lines found for request_id=${RID}" >&2
  echo "(check that the rid is correct and that 'docker compose logs' still has them)" >&2
  exit 1
fi

# Sort by timestamp — the request_id-aware format is:
#     {ts} {level} {request_id} {logger} {message}
# After the "svc | " prefix, the timestamp is field 3 (date) and field 4 (time).
# Sort by date+time as a single key.
sort -k3,3 -k4,4 "$TMP"
