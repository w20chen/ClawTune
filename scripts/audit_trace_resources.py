#!/usr/bin/env python3
"""Audit the swe-rebench JSONL trace for resource-monitoring correctness.

Checks two resource datasets:
  1. eBPF clause telemetry (execution.tool_resource, mode=clause) - clause granularity
  2. cgroup/process-tree resources (resources.*) - tool granularity
and the pid attribution chain that links them to a specific tool.
"""
import json
import sys
import re
from collections import Counter, defaultdict

TRACE = sys.argv[1] if len(sys.argv) > 1 else None

def load_lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

def deep_get(d, path):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur

def main():
    if not TRACE:
        print("usage: audit_trace_resources.py <trace.jsonl>")
        return
    lines = load_lines(TRACE)
    starts = [l for l in lines if l.get("record_type") == "span_start"]
    ends = [l for l in lines if l.get("record_type") == "span_end"]
    print(f"total lines={len(lines)} starts={len(starts)} ends={len(ends)}")
    print(f"kinds: {Counter(l.get('kind') for l in starts)}")
    print("=" * 100)

    tool_start = {l["span_id"]: l for l in starts if l.get("kind") == "tool"}
    tool_end = {l["span_id"]: l for l in ends if l.get("kind") == "tool"}

    issues = []
    rows = []
    for sid in tool_start:
        st = tool_start[sid]
        en = tool_end.get(sid)
        if not en:
            issues.append(f"[{sid}] tool span has no span_end")
            continue
        rows.append((sid, st, en))

    print(f"tool spans: {len(rows)}")
    print("=" * 100)

    for sid, st, en in rows:
        exec_ = en.get("execution") or {}
        tr = exec_.get("tool_resource") or {}
        summ = tr.get("artifact_summary") or {}
        coll = summ.get("collector") or {}
        res = en.get("resources") or {}
        ct = tr.get("call_telemetry") or {}
        clauses = ct.get("clauses") or []

        # --- execution / attribution chain ---
        mode = exec_.get("mode")
        payload_pid = exec_.get("payload_pid")
        pid_role = exec_.get("pid_role")
        src = exec_.get("source")
        cgroup_path = exec_.get("cgroup_path")
        pid_ticks = exec_.get("payload_pid_start_time_ticks")

        # --- resources (tool-level monitor) ---
        attr_status = res.get("attribution_status")
        attr_src = res.get("attribution_source")
        monitor_src = res.get("monitor_source")
        cov_ratio = res.get("coverage_ratio")
        cov_reason = res.get("coverage_reason")
        cov_dur = res.get("coverage_duration_ns")
        monitor_dur = res.get("monitor_duration_ns")
        cpu_time_s = res.get("cpu_time_s")
        cpu_avg_cores = res.get("cpu_utilization_avg_cores")
        cpu_avg_pct = res.get("cpu_utilization_avg_pct")
        cg_cpu_s = res.get("cgroup_cpu_time_s")
        mem_before = res.get("memory_rss_bytes_before")
        mem_after = res.get("memory_rss_bytes_after")
        procs_before = res.get("process_count_before")
        procs_after = res.get("process_count_after")
        ctx_delta = res.get("ctx_switches_delta")
        net_rx = res.get("net_rx_bytes_delta")
        net_tx = res.get("net_tx_bytes_delta")
        disk_r = res.get("disk_read_bytes_delta")
        disk_w = res.get("disk_write_bytes_delta")
        quality = res.get("quality")
        mon_start_mono = res.get("monitor_start_monotonic_ns")
        mon_end_mono = res.get("monitor_end_monotonic_ns")
        cgroup_art = res.get("cgroup_artifact_path")

        # --- clause telemetry ---
        clause_sum_cpu_ns = 0
        clause_peak_cpu = []
        clause_peak_mem = []
        clause_avail = Counter()
        for c in clauses:
            cpu_ns = c.get("cpu_ns_cumulative")
            if isinstance(cpu_ns, (int, float)):
                clause_sum_cpu_ns += cpu_ns
            if c.get("peak_cpu_cores") is not None:
                clause_peak_cpu.append(c["peak_cpu_cores"])
            if c.get("peak_memory_mb") is not None:
                clause_peak_mem.append(c["peak_memory_mb"])
            av = c.get("availability") or {}
            for k, v in av.items():
                clause_avail[f"{k}={v}"] += 1

        # ---- invariant checks ----
        issues_here = []

        # cgroup expectation: user wants cgroup granularity
        if cgroup_path is None:
            issues_here.append("execution.cgroup_path is null (expected cgroup path)")
        if cg_cpu_s is None:
            issues_here.append("resources.cgroup_cpu_time_s is null (expected cgroup cpu)")
        if monitor_src and "cgroup" not in monitor_src:
            issues_here.append(f"monitor_source='{monitor_src}' is NOT cgroup (expected cgroup, got process-tree fallback)")
        if cgroup_art and "cgroup" not in cgroup_art:
            issues_here.append(f"cgroup_artifact_path='{cgroup_art}' does not contain 'cgroup'")

        # attribution
        if attr_status != "attributed":
            issues_here.append(f"attribution_status='{attr_status}' (expected attributed)")
        if not payload_pid:
            issues_here.append(f"payload_pid missing/empty ({payload_pid})")

        # coverage logic
        if cov_ratio is not None and cov_ratio > 0 and cov_ratio < 0.99:
            issues_here.append(f"coverage_ratio={cov_ratio} ({cov_reason}) - window not fully covered")

        # cpu time consistency: cpu_time_s vs sum of clause cpu
        clause_cpu_s = clause_sum_cpu_ns / 1e9 if clause_sum_cpu_ns else 0.0
        if cpu_time_s is not None and clause_cpu_s and cpu_time_s == 0 and clause_cpu_s > 0.005:
            issues_here.append(f"resources.cpu_time_s=0.0 but clause cpu sum={clause_cpu_s:.4f}s - monitor missed CPU")
        if cpu_time_s is not None and cpu_time_s == 0 and monitor_dur and int(monitor_dur) > 0:
            issues_here.append(f"resources.cpu_time_s=0.0 while monitor ran {monitor_dur}ns - suspicious")

        # memory delta
        if mem_before is not None and mem_after is not None:
            if mem_before < 0 or mem_after < 0:
                issues_here.append(f"negative memory rss: before={mem_before} after={mem_after}")

        # duration invariants
        mono_s = en.get("monotonic_time_ns")
        dur_ns = en.get("duration_ns")
        if mono_s and dur_ns and st.get("monotonic_time_ns"):
            try:
                if int(mono_s) - int(st["monotonic_time_ns"]) != int(dur_ns):
                    issues_here.append(
                        f"monotonic invariant violated: start={st['monotonic_time_ns']} end={mono_s} "
                        f"delta={int(mono_s)-int(st['monotonic_time_ns'])} duration_ns={dur_ns}")
            except (TypeError, ValueError):
                pass

        # monitor window within tool window?
        try:
            tool_mono_start = int(st["monotonic_time_ns"])
            if mon_start_mono and int(mon_start_mono) < tool_mono_start:
                issues_here.append(f"monitor_start_monotonic_ns={mon_start_mono} < tool start {tool_mono_start}")
        except (TypeError, ValueError):
            pass

        # clause ts order
        for c in clauses:
            ts0, ts1 = c.get("ts_start"), c.get("ts_end")
            if ts0 is not None and ts1 is not None:
                if ts1 < ts0:
                    issues_here.append(f"clause {c.get('bin')} ts_end({ts1}) < ts_start({ts0})")

        # clause latency vs duration
        for c in clauses:
            lat = c.get("latency_ms")
            if lat is not None and lat < 0:
                issues_here.append(f"clause {c.get('bin')} latency_ms={lat} negative")

        # summary vs embedded
        call_count = summ.get("call_count")
        if call_count is not None and call_count != 1:
            issues_here.append(f"artifact call_count={call_count} != 1 (expected one call per tool span)")

        status_code = en.get("status", {}).get("code")
        exit_code = deep_get(en, ["output", "exit_code"])
        if exit_code is not None and status_code == "ok" and exit_code != 0:
            issues_here.append(f"status ok but exit_code={exit_code}")

        rows_out = {
            "span": sid, "tool": en.get("name"), "exec_mode": mode, "exec_id": exec_.get("execution_id"),
            "payload_pid": payload_pid, "pid_role": pid_role, "pid_source": src,
            "cgroup_path": cgroup_path, "pid_ticks": pid_ticks,
            "attr_status": attr_status, "attr_src": attr_src, "monitor_src": monitor_src,
            "coverage_ratio": cov_ratio, "coverage_reason": cov_reason,
            "monitor_dur_ns": monitor_dur, "coverage_dur_ns": cov_dur,
            "cpu_time_s": cpu_time_s, "cgroup_cpu_s": cg_cpu_s,
            "cpu_cores": cpu_avg_cores, "cpu_pct": cpu_avg_pct,
            "mem_before": mem_before, "mem_after": mem_after,
            "procs_before": procs_before, "procs_after": procs_after,
            "ctx_delta": ctx_delta, "net_rx": net_rx, "net_tx": net_tx,
            "disk_r": disk_r, "disk_w": disk_w, "quality": quality,
            "cgroup_art": cgroup_art,
            "clause_count": len(clauses), "clause_cpu_sum_s": round(clause_cpu_s, 6),
            "clause_peak_cpu": clause_peak_cpu, "clause_peak_mem": clause_peak_mem,
            "avail": dict(clause_avail), "issues": issues_here,
        }
        print(json.dumps(rows_out, ensure_ascii=False, sort_keys=True))

    print("=" * 100)
    all_issues = []
    for sid, st, en in rows:
        iss = (en.get("resources") or {}).get("_issues") or []
    # re-derive issue list from printed rows
    print("DONE")

if __name__ == "__main__":
    main()
