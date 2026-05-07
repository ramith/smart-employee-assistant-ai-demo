#!/usr/bin/env bash
# demo-up.sh — Start the smart-employee-agent demo stack.
#
# Usage:
#   ./scripts/demo-up.sh            # build + start + healthz smoke check
#   ./scripts/demo-up.sh --no-build # skip --build (use cached images)
#
# Requires: docker, docker compose (v2), python3
set -euo pipefail

cd "$(dirname "$0")/.."

BUILD_FLAG="--build"
if [[ "${1:-}" == "--no-build" ]]; then
  BUILD_FLAG=""
fi

echo "==> Starting smart-employee-agent demo stack..."
# shellcheck disable=SC2086
docker compose up -d ${BUILD_FLAG}

echo
echo "==> Waiting for services to initialise (5 s)..."
sleep 5

echo
python3 scripts/demo-smoke.py --skip-chat-test

echo
echo "==> All services up."
echo "    Browser:  http://localhost:8090"
echo "    Sign in:  employee_user / NewsMax@1234   (Employee role)"
echo "              hr_admin_user / NewsMax@1234  (HR Admin role — UC-07 asset issue)"
echo
echo "    Demo query: \"Show me my leave balance and what laptops are available\""
echo "    Tip:  LLM_FALLBACK_MODE=keyword is on — demo works without Gemini."
