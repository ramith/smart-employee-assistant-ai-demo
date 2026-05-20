#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./download_wso2_logs_rsync.sh [OUT_DIR] [SSH_KEY] [USER@HOST]
# Example:
#   ./download_wso2_logs_rsync.sh ./downloaded-logs \
#     /Users/ramith/demo/dda-poc/dda-poc-key.pem ubuntu@13.60.190.47

OUT_DIR="${1:-./downloaded-logs}"
SSH_KEY="${2:-/Users/ramith/demo/dda-poc/dda-poc-key.pem}"
REMOTE_HOST="${3:-ubuntu@13.60.190.47}"

mkdir -p "$OUT_DIR"

# Resolve the active WSO2 IS home on the remote host.
REMOTE_IS_HOME="$(ssh -i "$SSH_KEY" "$REMOTE_HOST" 'ls -d /home/ubuntu/wso2is-7.* 2>/dev/null | head -n 1')"

if [[ -z "$REMOTE_IS_HOME" ]]; then
  echo "ERROR: Could not find /home/ubuntu/wso2is-7.* on remote host: $REMOTE_HOST" >&2
  exit 1
fi

REMOTE_LOG_DIR="$REMOTE_IS_HOME/repository/logs"

echo "Remote log directory: $REMOTE_LOG_DIR"
echo "Local output directory: $OUT_DIR"

# Pull matching log files directly into a single local directory.
for pattern in "audit.log*" "http_access.log*" "wso2carbon.log*"; do
  echo "Syncing $pattern ..."
  rsync -av --progress \
    -e "ssh -i $SSH_KEY" \
    --rsync-path="sudo rsync" \
    "$REMOTE_HOST:$REMOTE_LOG_DIR/$pattern" \
    "$OUT_DIR/" || true
done

# Optional filtered snippet similar to your working command.
ssh -i "$SSH_KEY" "$REMOTE_HOST" \
  'sudo tail -200 /home/ubuntu/wso2is-7.*/repository/logs/audit.log 2>/dev/null | grep -i -E "logout|backchannel" | tail -30' \
  > "$OUT_DIR/access.log" || true

echo "Done. Files downloaded to: $OUT_DIR"
ls -lah "$OUT_DIR"
