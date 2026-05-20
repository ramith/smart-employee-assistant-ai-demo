#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

key="${2:-/Users/ramith/demo/dda-poc/dda-poc-key.pem}"
host="${3:-ubuntu@13.60.190.47}"
outdir="${1:-$SCRIPT_DIR}"

mkdir -p "$outdir"

echo "Syncing WSO2 logs to: $outdir"

is_home="$(ssh -i "$key" "$host" 'ls -d /home/ubuntu/wso2is-7.* 2>/dev/null | head -n 1')"

if [[ -z "$is_home" ]]; then
  echo "ERROR: Could not find /home/ubuntu/wso2is-7.* on $host" >&2
  exit 1
fi

remote_log_dir="$is_home/repository/logs"

for pattern in "audit.log*" "http_access.log*" "wso2carbon.log*"; do
  echo "Syncing $pattern ..."
  rsync -av --progress \
    -e "ssh -i $key" \
    --rsync-path="sudo rsync" \
    "$host:$remote_log_dir/$pattern" \
    "$outdir/" || true
done

# Filtered snippet from audit log, matching your working command behavior.
ssh -i "$key" "$host" \
  'sudo tail -200 /home/ubuntu/wso2is-7.*/repository/logs/audit.log 2>/dev/null | grep -i -E "logout|backchannel" | tail -30' \
  > "$outdir/access.log" || true

echo "Done. Logs are in: $outdir"
ls -lah "$outdir"
