"""Inspect the agent_datasets/swe-rebench tool-level resource format.

Usage::

    python scripts/inspect_swe_rebench_data.py <task_dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _peek(obj, depth=0, max_depth=3, prefix=""):
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            t = type(v).__name__
            if isinstance(v, (dict, list)):
                extra = f" ({len(v)} items)"
                print(f"{prefix}{k}: {t}{extra}")
                _peek(v, depth + 1, max_depth, prefix + "  ")
            else:
                print(f"{prefix}{k}: {t} = {v if not isinstance(v, str) or len(v) < 40 else v[:40] + '...'}")
    elif isinstance(obj, list) and obj:
        print(f"{prefix}[0] of {len(obj)}:")
        _peek(obj[0], depth + 1, max_depth, prefix + "  ")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    task_dir = Path(sys.argv[1])
    attempt = task_dir / "attempt_1"

    for name in ("resources.json", "tool_calls.json", "results.json", "run_manifest.json"):
        path = attempt / name
        if not path.is_file():
            print(f"-- {name}: MISSING --")
            continue
        print(f"\n===== {name} =====")
        data = json.loads(path.read_text(encoding="utf-8"))
        _peek(data, max_depth=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
