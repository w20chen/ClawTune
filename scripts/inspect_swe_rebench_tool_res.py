"""Check whether tool-level records carry resource fields in agent_datasets/swe-rebench.

Usage::

    python scripts/inspect_swe_rebench_tool_res.py <task_dir>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_RES_KEYS = (
    "mem", "memory", "rss", "peak_memory", "cpu", "disk", "net",
    "read_bytes", "write_bytes", "rx_bytes", "tx_bytes", "resource",
)


def _scan(obj, found: Counter, prefix: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            for rk in _RES_KEYS:
                if rk in kl:
                    found[rk] += 1
            if isinstance(v, (dict, list)):
                _scan(v, found, prefix + k + ".")
    elif isinstance(obj, list):
        for item in obj[:5]:
            if isinstance(item, (dict, list)):
                _scan(item, found, prefix)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    task_dir = Path(sys.argv[1])
    attempt = task_dir / "attempt_1"

    tc = attempt / "tool_calls.json"
    if tc.is_file():
        calls = json.loads(tc.read_text(encoding="utf-8"))
        tools = Counter(c.get("tool", "?") for c in calls if isinstance(c, dict))
        print(f"tool_calls.json: {len(calls)} records, tools={dict(tools)}")
        found = Counter()
        for c in calls:
            _scan(c, found)
        print(f"tool_calls.json resource-ish keys: {dict(found)}")
        # show full keys of an exec record if any
        execs = [c for c in calls if isinstance(c, dict) and c.get("tool", "").startswith("exec")]
        if execs:
            print(f"exec record keys: {sorted(execs[0].keys())}")
            print("exec[0] sample:", json.dumps(execs[0], ensure_ascii=False)[:800])
        else:
            print("no exec records in tool_calls.json")

    trace = attempt / "trace.jsonl"
    if trace.is_file():
        types = Counter()
        res_keys_by_type = {}
        n = 0
        with trace.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rt = rec.get("record_type") or rec.get("type") or "?"
                types[rt] += 1
                if rt not in res_keys_by_type:
                    found = Counter()
                    _scan(rec, found)
                    if found:
                        res_keys_by_type[rt] = dict(found)
        print(f"\ntrace.jsonl: {n} lines; record_type counts: {dict(types)}")
        for rt, res in res_keys_by_type.items():
            print(f"  {rt} resource-ish keys: {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
