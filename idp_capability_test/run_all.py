#!/usr/bin/env python3
"""
Run C0 → C2 → C3 → C5 in sequence. Stops on first FAIL.

Each test is invoked as a subprocess so its exit code drives flow.
This is a convenience wrapper — for debugging individual tests, run
them directly (`python c2_basic_token_exchange.py`) so you can `breakpoint()`
or step through.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

TESTS = [
    "c0_reachability.py",
    "c2_basic_token_exchange.py",
    "c3_nested_act.py",
    "c5_multi_resource.py",
]


def main() -> int:
    results: list[tuple[str, int]] = []
    for name in TESTS:
        print(f"\n{'='*70}\n  Running {name}\n{'='*70}")
        rc = subprocess.call([sys.executable, str(HERE / name)])
        results.append((name, rc))
        if rc != 0:
            print(f"\n>>> {name} FAILED (exit={rc}). Stopping.")
            break

    print("\n" + "=" * 70)
    print("  Summary")
    print("=" * 70)
    for name, rc in results:
        verdict = "PASS" if rc == 0 else f"FAIL (exit={rc})"
        print(f"  {name:35s}  {verdict}")
    return 0 if all(rc == 0 for _, rc in results) else 1


if __name__ == "__main__":
    sys.exit(main())
