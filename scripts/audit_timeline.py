#!/usr/bin/env python3
"""Inspect resource_timeline + full clause data for selected spans."""
import json
import sys

TRACE = sys.argv[1]
WANT = set(sys.argv[2:]) if len(sys.argv) > 2 else None

def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

lines = load(TRACE)
ends = [l for l in lines if l.get("record_type") == "span_end" and l.get("kind") == "tool"]

for en in ends:
    sid = en["span_id"]
    if WANT and sid not in WANT:
        continue
    res = en.get("resources") or {}
    exec_ = en.get("execution") or {}
    tr = exec_.get("tool_resource") or {}
    print("=" * 100)
    print(f"SPAN {sid} tool={en.get('name')} exec={exec_.get('execution_id')}")
    tl = res.get("resource_timeline") or []
    print(f"  resource_timeline: {len(tl)} points, truncated={res.get('resource_timeline_truncated')}")
    for i, p in enumerate(tl):
        print(f"    [{i}] ts={p.get('ts')} elapsed_ms={p.get('elapsed_ms')} cpu_delta_s={p.get('cpu_time_delta_s')} "
              f"rss={p.get('rss_bytes')} ctx_delta={p.get('ctx_switches_delta')} procs={p.get('process_count')} "
              f"avail={p.get('available')} src={p.get('source')} "
              f"rd={p.get('read_bytes_delta')} wr={p.get('write_bytes_delta')} rx={p.get('net_rx_bytes_delta')} tx={p.get('net_tx_bytes_delta')}")
    # clause data
    ct = tr.get("call_telemetry") or {}
    clauses = ct.get("clauses") or []
    print(f"  clauses: {len(clauses)}")
    for c in clauses:
        print(f"    bin={c.get('bin')!r} argv={c.get('argv')!r}")
        print(f"      ts=[{c.get('ts_start')} .. {c.get('ts_end')}] latency_ms={c.get('latency_ms')}")
        print(f"      cpu_ns_cumulative={c.get('cpu_ns_cumulative')} cumulative_cpu_s={c.get('cumulative_cpu_s')} "
              f"peak_cpu_cores={c.get('peak_cpu_cores')} peak_mem_mb={c.get('peak_memory_mb')}")
        print(f"      disk_r={c.get('disk_read_bytes')} disk_w={c.get('disk_write_bytes')} "
              f"net_rx={c.get('network_rx_bytes')} net_tx={c.get('network_tx_bytes')}")
        print(f"      status={json.dumps(c.get('status'))} availability={json.dumps(c.get('availability'))}")
    # summary
    summ = tr.get("artifact_summary") or {}
    print(f"  artifact_summary={json.dumps(summ, ensure_ascii=False)[:500]}")
