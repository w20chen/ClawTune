#!/usr/bin/env python3
"""Clause-level consistency checks + consolidated issue summary."""
import json
import sys
from collections import Counter

TRACE = sys.argv[1]

def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

lines = load(TRACE)
ends = {l["span_id"]: l for l in lines if l.get("record_type") == "span_end" and l.get("kind") == "tool"}
starts = {l["span_id"]: l for l in lines if l.get("record_type") == "span_start" and l.get("kind") == "tool"}

unit_mismatch = 0
ts_outside = 0
lat_mismatch = 0
empty_argv = 0
issues = []
clause_counts = []
psutil_cpu_zero_but_ebpf = 0
ebpf_total_cpu = 0.0
psutil_total_cpu = 0.0

for sid, en in ends.items():
    st = starts[sid]
    res = en.get("resources") or {}
    exec_ = en.get("execution") or {}
    tr = exec_.get("tool_resource") or {}
    ct = tr.get("call_telemetry") or {}
    clauses = ct.get("clauses") or []
    no_runtime = ct.get("no_runtime_exec") or tr.get("no_runtime_exec") or []
    clause_counts.append((sid, en.get("name"), len(clauses), len(no_runtime)))

    span_start_wall = int(st["wall_time_ns"]) / 1e9
    span_end_wall = int(en["wall_time_ns"]) / 1e9

    for c in clauses:
        cn = c.get("cpu_ns_cumulative")
        cs = c.get("cumulative_cpu_s")
        if cn is not None and cs is not None:
            if isinstance(cn, (int, float)) and isinstance(cs, (int, float)):
                if abs(cn / 1e9 - cs) > 1e-6:
                    unit_mismatch += 1
                    issues.append(f"[{sid}] {c.get('bin')}: cpu_ns={cn} != cumulative_cpu_s*1e9={cs*1e9}")
        ts0, ts1 = c.get("ts_start"), c.get("ts_end")
        if ts0 is not None and ts1 is not None:
            # clause window should be within tool span window (wall seconds)
            if ts0 < span_start_wall - 0.1 or ts1 > span_end_wall + 0.1:
                ts_outside += 1
                issues.append(f"[{sid}] {c.get('bin')} clause ts [{ts0:.3f},{ts1:.3f}] outside tool window [{span_start_wall:.3f},{span_end_wall:.3f}]")
            lat = c.get("latency_ms")
            if lat is not None and abs((ts1 - ts0) * 1000 - lat) > 100:
                lat_mismatch += 1
                issues.append(f"[{sid}] {c.get('bin')} latency_ms={lat:.2f} vs ts diff={(ts1-ts0)*1000:.2f}ms")
        argv = c.get("argv")
        if not argv or len(argv) == 0:
            empty_argv += 1
            issues.append(f"[{sid}] {c.get('bin')} empty argv")

    # cross-dataset cpu
    ebpf_cpu = sum((c.get("cpu_ns_cumulative") or 0) for c in clauses) / 1e9
    psutil_cpu = res.get("cpu_time_s")
    ebpf_total_cpu += ebpf_cpu
    if psutil_cpu is not None:
        psutil_total_cpu += psutil_cpu
    if (psutil_cpu == 0 or psutil_cpu is None) and ebpf_cpu > 0.005 and res.get("monitor_source") == "psutil-process-tree":
        psutil_cpu_zero_but_ebpf += 1

print("=== clause-level checks ===")
print(f"cpu_ns vs cumulative_cpu_s unit mismatches: {unit_mismatch}")
print(f"clause ts outside tool window: {ts_outside}")
print(f"latency_ms vs ts-diff mismatches: {lat_mismatch}")
print(f"empty argv: {empty_argv}")
print()
print("=== per-span clause counts (tool, clauses, no_runtime_exec) ===")
for sid, name, nc, nr in clause_counts:
    print(f"  {sid} {name}: clauses={nc} no_runtime_exec={nr}")
print()
print("=== cross-dataset cpu ===")
print(f"psutil cpu==0/None but eBPF clause cpu>5ms spans: {psutil_cpu_zero_but_ebpf} / {sum(1 for e in ends.values() if (e.get('resources') or {}).get('monitor_source')=='psutil-process-tree')}")
print(f"sum eBPF clause cpu = {ebpf_total_cpu:.2f}s; sum psutil cpu_time_s = {psutil_total_cpu:.2f}s")
print()
print("=== issues ===")
for i in issues:
    print(f"  - {i}")
print(f"total issue lines: {len(issues)}")
