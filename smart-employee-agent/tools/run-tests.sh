#!/usr/bin/env bash
# Sprint 1 test runner — runs each test file in isolation to avoid sys.modules
# pollution between test files that use importlib bootstraps. (Sprint 2 polish item.)
#
# Usage: ./tools/run-tests.sh [-v]
# Exits 0 if every file passes, 1 otherwise.

set -uo pipefail
cd "$(dirname "$0")/.."

# Activate venv if present
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

verbose=0
[ "${1:-}" = "-v" ] && verbose=1

pass=0
fail=0
failed_files=()
total_tests=0

for f in $(find tests -name 'test_*.py' | sort); do
    output=$(PYTHONPATH=. python3 -m pytest "$f" -q --override-ini="addopts=" 2>&1)
    last=$(echo "$output" | tail -1)
    if echo "$last" | grep -qE "passed"; then
        # extract test count
        n=$(echo "$last" | grep -oE "[0-9]+ passed" | head -1 | awk '{print $1}')
        total_tests=$((total_tests + n))
        pass=$((pass + 1))
        [ "$verbose" -eq 1 ] && echo "✓ $f ($n)"
    else
        fail=$((fail + 1))
        failed_files+=("$f")
        echo "✗ $f"
        echo "$output" | tail -10
    fi
done

echo
echo "===================================================="
echo "Files passed: $pass    Files failed: $fail"
echo "Total tests:  $total_tests"
echo "===================================================="

if [ "$fail" -gt 0 ]; then
    echo
    echo "Failed files:"
    printf '  %s\n' "${failed_files[@]}"
    exit 1
fi
