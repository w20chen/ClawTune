#!/usr/bin/env python3
"""Linux acceptance/overhead check for the Tool-level PMU collector.

The child remains behind the same exec-style gate used by managed Tool
execution.  Counters are armed against its trusted root PID before release;
the payload then execs and creates a descendant so inheritance is exercised.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "services" / "sidecar" / "src"
sys.path.insert(0, str(SIDECAR_SRC))

from clawtune_sidecar.monitoring.pmu import PmuCollector, auto_fd_budget  # noqa: E402


_GATED_EXEC = """
import os, sys
gate = int(sys.argv[1])
os.read(gate, 1)
os.close(gate)
os.execv(sys.executable, [sys.executable, "-c", sys.argv[2], sys.argv[3]])
"""

_WORKLOAD = """
import subprocess, sys
items = int(sys.argv[1])
child = '''
import sys
items = int(sys.argv[1])
data = bytearray(16 * 1024 * 1024)
position = 1
total = 0
for index in range(items):
    position = (position * 1103515245 + 12345) & 0x00ffffff
    total += data[position]
print(total)
'''
raise SystemExit(subprocess.run(
    [sys.executable, "-c", child, str(items)],
    stdout=subprocess.DEVNULL,
).returncode)
"""


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _run_batch(*, count: int, concurrency: int, collector: PmuCollector,
               work_items: int, prefix: str) -> dict[str, Any]:
    latencies: list[float] = []
    profiles: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    maximum_active = 0
    batch_started = time.perf_counter()

    for offset in range(0, count, concurrency):
        wave: list[tuple[str, subprocess.Popen[bytes], int, float]] = []
        for index in range(offset, min(offset + concurrency, count)):
            read_fd, write_fd = os.pipe()
            os.set_inheritable(read_fd, True)
            execution_id = f"{prefix}-{index}"
            admitted_at = time.perf_counter()
            process = subprocess.Popen(
                [sys.executable, "-c", _GATED_EXEC, str(read_fd), _WORKLOAD,
                 str(work_items)],
                pass_fds=(read_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            os.close(read_fd)
            profile = collector.begin(execution_id, process.pid)
            if profile is not None:
                profiles.append(profile.to_dict())
            wave.append((execution_id, process, write_fd, admitted_at))
        maximum_active = max(maximum_active, collector.diagnostics()["active"])
        for _execution_id, _process, write_fd, _admitted_at in wave:
            os.write(write_fd, b"1")
            os.close(write_fd)

        def wait_one(item: tuple[str, subprocess.Popen[bytes], int, float]):
            execution_id, process, _write_fd, admitted_at = item
            _stdout, stderr = process.communicate()
            profile = collector.finish(execution_id)
            collector.take(execution_id)
            return execution_id, process.pid, process.returncode, stderr, (
                time.perf_counter() - admitted_at
            ), profile.to_dict()

        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            for execution_id, root_pid, returncode, stderr, elapsed, profile in pool.map(
                wait_one, wave
            ):
                if not any(item.get("execution_id") == execution_id for item in profiles):
                    profiles.append(profile)
                latencies.append(elapsed)
                if returncode != 0:
                    failures.append({
                        "execution_id": execution_id,
                        "returncode": returncode,
                        "stderr": stderr.decode(errors="replace")[-1000:],
                    })
                if profile.get("root_pid") not in {None, root_pid}:
                    failures.append({
                        "execution_id": execution_id,
                        "error": "profile root_pid attribution mismatch",
                    })
                if profile.get("execution_id") != execution_id:
                    failures.append({
                        "execution_id": execution_id,
                        "error": "profile execution_id attribution mismatch",
                    })
                if set(profile.get("events", {})) != {
                    "cycles", "instructions", "llc_read_misses", "llc_read_accesses",
                }:
                    failures.append({
                        "execution_id": execution_id,
                        "error": "profile event set mismatch",
                    })

    elapsed = time.perf_counter() - batch_started
    status_counts = Counter(
        str(profile.get("coverage", {}).get("status")) for profile in profiles
    )
    reason_counts = Counter(
        str(profile.get("coverage", {}).get("reason")) for profile in profiles
    )
    return {
        "count": count,
        "concurrency": concurrency,
        "elapsed_s": elapsed,
        "throughput_per_s": count / elapsed,
        "latency_ms": {
            "median": statistics.median(latencies) * 1000,
            "p95": _percentile(latencies, 0.95) * 1000,
        },
        "maximum_active_groups": maximum_active,
        "maximum_active_fds": maximum_active * 4,
        "coverage_statuses": dict(status_counts),
        "coverage_reasons": dict(reason_counts),
        "failures": failures,
        "profiles": profiles,
    }


def _overhead(off: dict[str, Any], on: dict[str, Any]) -> dict[str, float]:
    off_throughput = float(off["throughput_per_s"])
    on_throughput = float(on["throughput_per_s"])
    off_median = float(off["latency_ms"]["median"])
    on_median = float(on["latency_ms"]["median"])
    return {
        "throughput_impact_percent": 100.0 * (off_throughput - on_throughput) / off_throughput,
        "median_latency_impact_percent": 100.0 * (on_median - off_median) / off_median,
        "median_latency_delta_ms": on_median - off_median,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-active", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--high-concurrency", type=int, default=32)
    parser.add_argument("--benchmark-count", type=int, default=16)
    parser.add_argument("--work-items", type=int, default=400_000)
    parser.add_argument("--reliable-ratio", type=float, default=0.95)
    parser.add_argument("--require-reliable", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if platform.system().lower() != "linux":
        print(json.dumps({"status": "unsupported", "reason": "Linux required"}))
        return 2
    if min(args.max_active, args.concurrency, args.high_concurrency,
           args.benchmark_count, args.work_items) <= 0:
        parser.error("all numeric workload and concurrency values must be positive")

    def collector(enabled: bool) -> PmuCollector:
        return PmuCollector(
            enabled=enabled,
            max_active=args.max_active,
            max_fds=auto_fd_budget(args.max_active),
            reliable_ratio=args.reliable_ratio,
        )

    # Warm the interpreter/page cache outside the measured comparison.
    _run_batch(count=1, concurrency=1, collector=collector(False),
               work_items=max(1, args.work_items // 4), prefix="warmup")
    single = _run_batch(
        count=1, concurrency=1, collector=collector(True),
        work_items=args.work_items, prefix="single",
    )
    concurrent = _run_batch(
        count=args.concurrency, concurrency=args.concurrency, collector=collector(True),
        work_items=args.work_items, prefix="concurrent",
    )
    high = _run_batch(
        count=args.high_concurrency, concurrency=args.high_concurrency,
        collector=collector(True), work_items=args.work_items, prefix="high",
    )
    off = _run_batch(
        count=args.benchmark_count, concurrency=args.concurrency,
        collector=collector(False), work_items=args.work_items, prefix="off",
    )
    on = _run_batch(
        count=args.benchmark_count, concurrency=args.concurrency,
        collector=collector(True), work_items=args.work_items, prefix="on",
    )
    failures = sum(
        (result["failures"] for result in (single, concurrent, high, off, on)), []
    )
    budget_rejections = high["coverage_reasons"].get("resource_budget", 0)
    event_groups_available = any(
        status in single["coverage_statuses"]
        for status in ("reliable", "multiplexed", "partial")
    )
    expected_rejections = max(0, args.high_concurrency - args.max_active)
    budget_ok = (
        high["maximum_active_groups"] <= args.max_active
        and high["maximum_active_fds"] <= auto_fd_budget(args.max_active)
        and (
            not event_groups_available
            or budget_rejections >= expected_rejections
        )
    )
    reliable_single = single["coverage_statuses"].get("reliable", 0) == 1
    report = {
        "schema": "pmu_validation_v1",
        "status": "ok" if not failures and budget_ok else "failed",
        "host": {"architecture": platform.machine(), "cpu_count": os.cpu_count()},
        "collector": collector(True).diagnostics(),
        "checks": {
            "single_tool_reliable": reliable_single,
            "all_tools_completed": not failures,
            "high_concurrency_budget_bounded": budget_ok,
            "high_concurrency_budget_rejections": budget_rejections,
            "fixed_event_fds_per_active_execution": 4,
        },
        "single": single,
        "concurrent": concurrent,
        "high_concurrency": high,
        "pmu_off": off,
        "pmu_on": on,
        "overhead": _overhead(off, on),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if report["status"] != "ok":
        return 1
    if args.require_reliable and not reliable_single:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
