"""Scan clause_telemetry / trace for memory / network / disk fields and availability.

Usage::

    python scripts/inspect_resource_fields.py <dataset_root> [max_tasks]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_DISK_FIELDS = ("disk_io",)
_NET_FIELDS = ("net", "network", "net_rx_bytes", "net_tx_bytes", "net_rx_mb", "net_tx_mb")
_MEM_FIELDS = ("sampled_peak_rss_mb", "peak_memory_mb", "memory_mb", "rss")


def _avail_stats(clause: dict) -> dict[str, Counter]:
    out = {k: Counter() for k in ("latency", "cpu", "memory", "disk_io", "net")}
    availability = clause.get("availability") or {}
    for key in ("latency", "cpu", "memory", "disk_io", "net"):
        val = availability.get(key)
        out[key][str(val)] += 1
    # field presence
    for f in _NET_FIELDS:
        if f in clause:
            out["net"]["present:" + f] += 1
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    max_tasks = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    task_dirs = sorted(d for d in root.iterdir() if d.is_dir())[:max_tasks]

    totals = {k: Counter() for k in ("latency", "cpu", "memory", "disk_io", "net")}
    clauses_seen = 0
    tool_calls_seen = 0
    call_net = 0
    call_mem = 0
    call_disk = 0
    resources_disabled = 0
    resources_samples = 0

    for task_dir in task_dirs:
        for attempt in sorted(task_dir.iterdir()):
            if not attempt.is_dir() or not attempt.name.startswith("attempt_"):
                continue
            ct = attempt / "clause_telemetry.json"
            if ct.is_file():
                try:
                    artifact = json.loads(ct.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for call in artifact.get("calls", []):
                    for clause in call.get("clauses", []):
                        clauses_seen += 1
                        for k, c in _avail_stats(clause).items():
                            totals[k].update(c)
            trace = attempt / "trace.jsonl"
            if trace.is_file():
                try:
                    for line in trace.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        if rec.get("action_type") != "tool_exec":
                            continue
                        tool_calls_seen += 1
                        data = rec.get("data") or {}
                        if any(f in data for f in _NET_FIELDS):
                            call_net += 1
                        if any(f in data for f in _MEM_FIELDS):
                            call_mem += 1
                        if any(f in data for f in _DISK_FIELDS):
                            call_disk += 1
                except Exception:
                    pass
            res = attempt / "resources.json"
            if res.is_file():
                try:
                    rj = json.loads(res.read_text(encoding="utf-8"))
                except Exception:
                    continue
                summary = rj.get("summary", {})
                mon = summary.get("monitoring", {})
                if mon.get("status") == "disabled" or summary.get("monitoring_disabled"):
                    resources_disabled += 1
                resources_samples += len(rj.get("samples", []) or [])

    print(f"tasks scanned: {len(task_dirs)} | clauses: {clauses_seen} | tool_exec calls: {tool_calls_seen}")
    for key in ("latency", "cpu", "memory", "disk_io", "net"):
        print(f"  clause availability[{key}]: {dict(totals[key])}")
    print(f"  resources.json: disabled={resources_disabled}, total samples={resources_samples}")
    print(f"  tool_exec records with net field: {call_net} | mem field: {call_mem} | disk field: {call_disk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
