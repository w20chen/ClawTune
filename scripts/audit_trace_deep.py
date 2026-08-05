#!/usr/bin/env python3
"""Deep audit: wall-clock coverage math, monitor duration consistency,
clause telemetry internals, cross-dataset consistency."""
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

print(f"tool spans: {len(ends)}")
print("=" * 110)

for sid in sorted(ends, key=lambda s: starts[s]["sequence_no"] if s in starts else 0):
    en = ends[sid]
    st = starts.get(sid)
    res = en.get("resources") or {}
    exec_ = en.get("execution") or {}
    tr = exec_.get("tool_resource") or {}
    summ = tr.get("artifact_summary") or {}
    ct = tr.get("call_telemetry") or {}
    clauses = ct.get("clauses") or []

    span_start_wall = int(st["wall_time_ns"]) if st else None
    span_end_wall = int(en["wall_time_ns"])
    span_start_mono = int(st["monotonic_time_ns"]) if st else None
    span_end_mono = int(en["monotonic_time_ns"])
    dur_ns = int(en["duration_ns"])
    mon_start_wall = res.get("monitor_start_wall_time_ns")
    mon_end_wall = res.get("monitor_end_wall_time_ns")
    mon_start_mono = res.get("monitor_start_monotonic_ns")
    mon_end_mono = res.get("monitor_end_monotonic_ns")
    mon_dur_ns = res.get("monitor_duration_ns")
    cov_dur_ns = res.get("coverage_duration_ns")
    cov_ratio = res.get("coverage_ratio")
    cov_reason = res.get("coverage_reason")
    action_dur = res.get("action_duration_ns")
    plugin_window = res.get("plugin_window_ns")
    tool_body = res.get("tool_body_ns")
    decision = res.get("decision_duration_ns")
    completion = res.get("completion_duration_ns")

    # expected monitor duration from wall times
    exp_mon_wall = None
    if mon_start_wall and mon_end_wall:
        exp_mon_wall = int(mon_end_wall) - int(mon_start_wall)
    exp_mon_mono = None
    if mon_start_mono and mon_end_mono:
        exp_mon_mono = int(mon_end_mono) - int(mon_start_mono)

    # expected coverage from wall times (trace.py _coverage formula)
    exp_cov = None
    exp_ratio = None
    if mon_start_wall and mon_end_wall and span_start_wall is not None:
        overlap = max(0, min(span_end_wall, int(mon_end_wall)) - max(span_start_wall, int(mon_start_wall)))
        exp_cov = overlap
        if dur_ns > 0:
            exp_ratio = min(1.0, max(0.0, overlap / dur_ns))

    # clause checks
    clause_ts_ok = True
    clause_lat_ok = True
    for c in clauses:
        ts0, ts1 = c.get("ts_start"), c.get("ts_end")
        if ts0 is not None and ts1 is not None and ts1 < ts0:
            clause_ts_ok = False
        lat = c.get("latency_ms")
        if lat is not None and lat < 0:
            clause_lat_ok = False
        cn = c.get("cpu_ns_cumulative")
        cs = c.get("cumulative_cpu_s")
        if cn is not None and cs is not None and isinstance(cn, (int, float)) and isinstance(cs, (int, float)):
            if abs(cn / 1e9 - cs) > 1e-6:
                print(f"  [clause-unit] {sid} bin={c.get('bin')} cpu_ns={cn} cumulative_cpu_s={cs} mismatch")

    # cpu consistency: eBPF clause cpu vs psutil cpu_time_s
    clause_cpu = sum((c.get("cpu_ns_cumulative") or 0) for c in clauses) / 1e9
    cpu_time_s = res.get("cpu_time_s")

    # summary of available clause data
    clause_bins = [c.get("bin") for c in clauses]

    print(f"\n--- {sid} tool={en.get('name')} exec={exec_.get('execution_id')} mode={exec_.get('mode')}")
    print(f"    span wall: [{span_start_wall} .. {span_end_wall}] dur_ns={dur_ns} mono_end-mono_start={span_end_mono-span_start_mono}")
    print(f"    monitor wall: [{mon_start_wall} .. {mon_end_wall}] dur_ns={mon_dur_ns} exp_wall={exp_mon_wall} exp_mono={exp_mon_mono}")
    print(f"    monitor_start_mono BEFORE span_start_mono? {mon_start_mono and span_start_mono and int(mon_start_mono) < span_start_mono}")
    print(f"    coverage: reported={cov_dur_ns} ratio={cov_ratio} reason={cov_reason}")
    print(f"             expected_from_wall={exp_cov} ratio={exp_ratio}")
    print(f"    action_duration_ns={action_dur} plugin_window={plugin_window} tool_body={tool_body} decision={decision} completion={completion}")
    print(f"    cpu: psutil_cpu_time_s={cpu_time_s}  eBPF_clause_cpu_s={clause_cpu:.4f}  diff={clause_cpu-(cpu_time_s or 0):.4f}")
    print(f"    clauses={clause_bins} clause_ts_ok={clause_ts_ok} clause_lat_ok={clause_lat_ok}")
    print(f"    artifact: mode={summ.get('mode')} schema={summ.get('schema')} quality={summ.get('telemetry_quality')} "
          f"completeness={summ.get('formal_completeness')} container={summ.get('container_id')} "
          f"kprobe_hits={ (summ.get('collector') or {}).get('kprobe_total_hits') }")
    # resources quality / attribution
    print(f"    resources: quality={res.get('quality')} attr_status={res.get('attribution_status')} "
          f"attr_src={res.get('attribution_source')} monitor_src={res.get('monitor_source')} scope={res.get('scope')} "
          f"procs={res.get('process_count_before')}->{res.get('process_count_after')}")
