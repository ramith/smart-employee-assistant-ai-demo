#!/usr/bin/env bash
# grep-trace.sh — reconstruct an end-to-end request trace across the demo stack.
#
# Sprint 3 3A.4. The orchestrator stamps every inbound /auth/logout with a
# correlation id like ``logout-zy99h28g-moz8zrn3``. That rid is propagated to
# the four /internal/events receivers (hr_agent, it_agent, hr_server,
# it_server) via X-Request-ID, and reappears on each receiver's log line.
# This script pulls every log line carrying a given rid from all five
# services and prints them chronologically — the demo's "show me the trace"
# answer to "did the cascade actually fan out?".
#
# Usage:
#   ./tools/grep-trace.sh <rid> [--since <docker-since-spec>]
#   ./tools/grep-trace.sh                 # no rid → list recent logout rids
#
# Examples:
#   ./tools/grep-trace.sh logout-zy99h28g-moz8zrn3
#   ./tools/grep-trace.sh logout-zy99h28g-moz8zrn3 --since 10m
#
# Exits 0 on success, 1 if no matching lines found, 2 on argument error.

set -uo pipefail
cd "$(dirname "$0")/.."

# Five services that participate in a UC-09 logout cascade.
readonly SERVICES=(orchestrator hr_agent it_agent hr_server it_server)

# Default lookback — keeps the grep cheap on long-running stacks.
SINCE="30m"

# ── Arg parse ────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    echo "usage: $0 <rid> [--since <duration>]" >&2
    echo "       $0           # list recent logout rids" >&2
    echo
    echo "recent logout rids (last $SINCE):" >&2
    docker compose logs --since "$SINCE" orchestrator 2>/dev/null \
        | grep -oE 'logout-[a-z0-9-]{8,}' \
        | sort -u \
        | tail -10
    exit 0
fi

RID="$1"; shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --since) SINCE="$2"; shift 2 ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

# Reject obvious garbage early — the rid should be a non-empty string with
# no shell metacharacters that would confuse the grep below.
if [ -z "$RID" ] || echo "$RID" | grep -qE '[[:space:]"\$\\\\]'; then
    echo "invalid rid: $RID" >&2
    exit 2
fi

# ── Collect lines ────────────────────────────────────────────────────────────
# Tag each line with its source service so the merged output stays readable
# after the timestamp sort drops the per-stream order.

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

found=0
for svc in "${SERVICES[@]}"; do
    # docker compose logs prefixes lines with "<svc>-1  | …". Strip that and
    # add our own ``[svc]`` tag so all rows align after sort.
    docker compose logs --since "$SINCE" "$svc" 2>/dev/null \
        | grep -F "$RID" \
        | sed -E "s/^[^|]*\| /[$svc] /" \
        >> "$tmp" || true
    if [ -s "$tmp" ]; then
        found=1
    fi
done

if [ "$found" -eq 0 ]; then
    echo "no log lines matched rid=$RID in the last $SINCE" >&2
    exit 1
fi

# ── Sort and emit ────────────────────────────────────────────────────────────
# All five services share the same ISO-8601-ish timestamp prefix
# (``YYYY-MM-DD HH:MM:SS,mmm``) immediately after the ``[svc] `` tag, so a
# plain lexical sort on the column after the tag yields chronological order.
# We sort on the timestamp field (col 2 = date, col 3 = time).
sort -k2,2 -k3,3 "$tmp"

# Hop-coverage summary — the audience-friendly answer to "did all four
# receivers see it?". One-line per service with a tick or cross.
echo
echo "── hop coverage ──"
for svc in "${SERVICES[@]}"; do
    if grep -qE "^\[$svc\]" "$tmp"; then
        printf "  %s  %s\n" "✓" "$svc"
    else
        printf "  %s  %s  (no lines)\n" "✗" "$svc"
    fi
done
