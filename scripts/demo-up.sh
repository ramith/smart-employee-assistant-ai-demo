#!/usr/bin/env bash
# demo-up.sh — Start the smart-employee-agent demo stack reliably.
#
# Usage:
#   ./scripts/demo-up.sh            # build (cached layers) + start + healthz smoke
#   ./scripts/demo-up.sh --clean    # down + no-cache rebuild ALL images + start + smoke
#   ./scripts/demo-up.sh --no-build # skip build entirely (use existing images)
#
# Five services come up: orchestrator (8090), hr_agent, hr_server,
# it_agent, it_server. (The pre-v4 `agent`/`client` services were removed.)
#
# Requires: docker, docker compose (v2), python3
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-}"

case "$MODE" in
  --clean)
    echo "==> Clean rebuild: tearing down (incl. orphans), rebuilding all images --no-cache..."
    docker compose down --remove-orphans
    docker compose build --no-cache
    echo "==> Starting demo stack (freshly built images)..."
    docker compose up -d
    ;;
  --no-build)
    echo "==> Starting demo stack (existing images, no build)..."
    docker compose up -d
    ;;
  "")
    echo "==> Starting demo stack (build with cached layers)..."
    docker compose up -d --build
    ;;
  *)
    echo "unknown option: $MODE" >&2
    echo "usage: $0 [--clean | --no-build]" >&2
    exit 2
    ;;
esac

echo
echo "==> Waiting for services to initialise (6 s)..."
sleep 6

echo
# --skip-chat-test: the smoke script only verifies healthz endpoints; the
# full chat flow is exercised by the manual gate (Stage 11 runbook).
python3 scripts/demo-smoke.py --skip-chat-test || {
  echo
  echo "!! demo-smoke reported a failure. Check 'docker compose ps' and"
  echo "   'docker compose logs <service>'. Common causes:"
  echo "     - stale agent secret  -> regenerate in IS Console, update the"
  echo "       service .env (ORCHESTRATOR_AGENT_SECRET / HR_AGENT_SECRET /"
  echo "       IT_AGENT_SECRET), then re-run with --no-build."
  echo "     - secret drift across containers -> re-run with --clean."
  echo "     - IS not reachable    -> ./scripts/check-is-config.py"
  exit 1
}

echo
echo "==> All services up."
echo "    Browser:  http://localhost:8090"
echo "    Sign in:  employee@example.com / NewsMax@1234   (Employee — sidebar self-service)"
echo "              hradmin@example.com  / NewsMax@1234   (HR Admin — Reports page + cubicle assign)"
echo
echo "    Demo queries (natural language — the LLM router extracts the args):"
echo "      employee : \"How much annual leave do I have, and where's my cubicle?\""
echo "                 \"I'd like annual leave from 2026-06-10 to 2026-06-14, reason: family trip\""
echo "      hr_admin : \"Show me vacant cubicles\"  then  \"floor 2\"  then  \"assign C-027 to jane.doe\""
echo
echo "    Routing: LLM_FALLBACK_MODE in orchestrator/.env (currently set to 'llm' if an"
echo "    OPENAI_API_KEY is configured there — OpenAI via the WSO2 AI Gateway). The keyword"
echo "    router stays wired as the automatic fallback — if OpenAI / the WSO2 AI Gateway is"
echo "    unreachable / rate-limited / the key is invalid, the chat degrades to the"
echo "    Sprint-4 keyword behaviour, never a hard error."
echo
echo "    Pre-flight IS check (run if anything 401s): ./scripts/check-is-config.py"
echo "    Manual gate runbook: docs/architecture/sprint-5-stage-11-manual-gate.md"
