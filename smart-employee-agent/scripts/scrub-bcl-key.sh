#!/usr/bin/env bash
# scripts/scrub-bcl-key.sh — secure-delete extracted IS signing key material.
#
# Counterpart to scripts/synth-bcl.py. Run after every demo where you
# extracted the WSO2 IS RS256 signing key from the AWS VM keystore. The
# extracted PEM signs every JWT IS issues — leakage is full-IdP-key
# compromise, not just BCL-scoped.
#
# Default targets: anything under _local/ plus the canonical extraction
# paths from synth-bcl.py's docstring. Operator can pass extra paths as
# args.
#
# Usage:
#   ./scripts/scrub-bcl-key.sh                   # default targets
#   ./scripts/scrub-bcl-key.sh /tmp/my_extra.pem # extra paths

set -uo pipefail
cd "$(dirname "$0")/.."

# Targets the operator might have written to.
TARGETS=(
    "./_local/wso2_private.pem"
    "./_local/wso2carbon.p12"
    "/tmp/wso2_private.pem"
    "/tmp/wso2carbon.p12"
)
TARGETS+=("$@")

# Detect a secure-delete tool. shred is GNU; macOS rm supports -P
# (overwrite before unlink). Fall back to plain rm with a WARN.
if command -v shred >/dev/null 2>&1; then
    DELETE_CMD=(shred -u -n 3)
elif [ "$(uname -s)" = "Darwin" ]; then
    DELETE_CMD=(rm -P)
else
    echo "warn: no shred and not Darwin; falling back to plain rm (key remnants may persist on disk)" >&2
    DELETE_CMD=(rm -f)
fi

scrubbed=0
for path in "${TARGETS[@]}"; do
    if [ -f "$path" ]; then
        echo "scrub $path"
        "${DELETE_CMD[@]}" "$path" 2>/dev/null || rm -f "$path"
        scrubbed=$((scrubbed + 1))
    fi
done

# Also wipe any *.pem that may have been left under _local/ by other paths.
if [ -d "./_local" ]; then
    while IFS= read -r -d '' f; do
        echo "scrub $f"
        "${DELETE_CMD[@]}" "$f" 2>/dev/null || rm -f "$f"
        scrubbed=$((scrubbed + 1))
    done < <(find ./_local -type f \( -name '*.pem' -o -name '*.p12' \) -print0 2>/dev/null)
fi

if [ "$scrubbed" -eq 0 ]; then
    echo "no key material found at expected paths — nothing to do."
else
    echo "scrubbed $scrubbed file(s)."
fi
