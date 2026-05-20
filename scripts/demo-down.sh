#!/usr/bin/env bash
# demo-down.sh — Tear down the smart-employee-agent demo stack.
#
# Usage:
#   ./scripts/demo-down.sh           # stop + remove containers; keep volumes
#   ./scripts/demo-down.sh --volumes # also remove named volumes
set -euo pipefail

cd "$(dirname "$0")/.."

VOLUMES_FLAG=""
if [[ "${1:-}" == "--volumes" ]]; then
  VOLUMES_FLAG="--volumes"
fi

echo "==> Tearing down smart-employee-agent demo stack..."
# shellcheck disable=SC2086
docker compose down ${VOLUMES_FLAG}
echo "==> Done."
