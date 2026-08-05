"""Analyze span timing for a single OpenClaw v6 trace JSONL.

Goal: separate "pure work" from harness/overhead for tool spans, especially
read/edit where the recorded "tool time" looks too big.

Recorded fields (span_end):
- duration_ns / resources.action_duration_ns: the duration reported by
  OpenClaw for the actual tool action (wall clock, includes tool-internal
  startup/serialization/blocked time; scheduler round-trips are outside it).
- resources.cpu_time_s: real CPU time consumed in the monitored scope
  (cgroup/process tree).  This is the closest "pure work" figure.
- resources.coverage_ratio: fraction of the action covered by the monitor.

Interpretation ("option 0", no code change):
- pure-work proxy  = cpu_time_s, valid only when coverage_ratio is high AND
  attribution is not container-wide.
- overhead (gap)   = action_duration_s - cpu_time_s (blocked/startup inside
  the OpenClaw action) + scheduler round-trips (when plugin_window_ns and
  scheduler_overhead_ns are recorded).
"""
import json
import sys
from collections import defaultdict


def load_spans(path):
    starts = {}
    ends = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rt = rec.get("record_type")
            if rt == "span_start":
                starts[rec["span_id"]] = rec
            elif rt == "span_end":
                ends[rec["span_id"]] = rec
    return starts, ends


def to_ns(v):
    return int(v)


def main(path):
    starts, ends = load_spans(path)

    rows = []
    for sid, end in ends.items():
        start = starts.get(sid)
        if not start:
            continue
        kind = start.get("kind")
        name = start.get("name")
        res = end.get("resources") or {}

        action_ns = to_ns(res.get("action_duration_ns") or end.get("duration_ns", 0))
        cov_ns = to_ns(res.get("coverage_duration_ns") or 0)
        cpu_s = res.get("cpu_time_s")

        rows.append(
            {
                "span_id": sid,
                "kind": kind,
                "name": name,
                "action_s": action_ns / 1e9,
                "cov_s": cov_ns / 1e9 if cov_ns else None,
                "cpu_s": cpu_s if cpu_s is not None else None,
                "util_pct": res.get("cpu_utilization_avg_pct"),
                "cov_ratio": res.get("coverage_ratio"),
                "qual": res.get("quality"),
                "attrib": res.get("attribution_status"),
                "attrib_src": res.get("attribution_source"),
                "tool_body_s": _opt_s(res.get("tool_body_ns")),
                "sched_overhead_s": _opt_s(res.get("scheduler_overhead_ns")),
                "plugin_window_s": _opt_s(res.get("plugin_window_ns")),
            }
        )

    print("== Per tool span ==")
    print(
        f"{'tool':<8}{'action_s':>9}{'cpu_s':>8}{'gap_s':>8}{'cov%':>7}"
        f"{'tool_body':>10}{'sched_oh':>10}  attrib / source"
    )
    print("-" * 104)
    for r in sorted([x for x in rows if x["kind"] == "tool"], key=lambda x: -x["action_s"]):
        gap = r["action_s"] - (r["cpu_s"] or 0)
        covpct = 100.0 * (r["cov_ratio"] or 0)
        tb = f"{r['tool_body_s']:.3f}" if r["tool_body_s"] is not None else "-"
        so = f"{r['sched_overhead_s']:.3f}" if r["sched_overhead_s"] is not None else "-"
        print(
            f"{r['name']:<8}{r['action_s']:>9.3f}{(r['cpu_s'] or 0):>8.3f}{gap:>8.3f}"
            f"{covpct:>7.1f}{tb:>10}{so:>10}  {r['qual']} / {r['attrib']} ({r['attrib_src']})"
        )

    print("\n== Per tool-name summary ==")
    byname = defaultdict(list)
    for r in rows:
        if r["kind"] == "tool":
            byname[r["name"]].append(r)
    for k, lst in sorted(byname.items()):
        n = len(lst)
        act = sum(x["action_s"] for x in lst)
        cpu = sum(x["cpu_s"] or 0 for x in lst)
        tb = sum(x["tool_body_s"] for x in lst if x["tool_body_s"] is not None)
        so = sum(x["sched_overhead_s"] for x in lst if x["sched_overhead_s"] is not None)
        print(
            f"{k:<10} n={n:<3} total_action={act:>7.2f}s total_cpu={cpu:>7.2f}s "
            f"gap={act-cpu:>7.2f}s" + (f"  tool_body={tb:>7.2f}s sched_oh={so:>7.2f}s" if so else "")
        )

    print("\n== Pure-work interpretation (option 0) ==")
    print(
        "pure-work proxy = cpu_time_s (real CPU). Valid when coverage_ratio is "
        "high and attribution is NOT whole-container cgroup "
        "(shared-sandbox-container includes other processes: exec cpu can exceed action)."
    )


def _opt_s(v):
    if v is None:
        return None
    try:
        return int(v) / 1e9
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main(sys.argv[1])
