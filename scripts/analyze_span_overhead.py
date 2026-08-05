"""Analyze span timing for a single OpenClaw trace JSONL.

Focus: explain the gap between wall duration and CPU time for tool spans,
especially read/edit tools (where the user reports "tool time" looks too big).
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

        dur_ns = to_ns(end.get("duration_ns", 0))
        action_ns = to_ns(res.get("action_duration_ns") or 0)
        cov_ns = to_ns(res.get("coverage_duration_ns") or 0)
        cpu_s = res.get("cpu_time_s")

        rows.append(
            {
                "span_id": sid,
                "kind": kind,
                "name": name,
                "dur_s": dur_ns / 1e9,
                "action_s": action_ns / 1e9,
                "cov_s": cov_ns / 1e9 if cov_ns else None,
                "cpu_s": cpu_s if cpu_s is not None else None,
                "util_pct": res.get("cpu_utilization_avg_pct"),
                "qual": res.get("quality"),
                "attrib": res.get("attribution_status"),
            }
        )

    rows.sort(key=lambda r: r["dur_s"], reverse=True)

    print(f"{'kind':<7}{'name':<12}{'dur_s':>8}{'action_s':>9}{'cpu_s':>8}{'noncpu_s':>9}{'util%':>7}{'cov%':>7}  qual/attrib")
    print("-" * 100)
    for r in rows:
        if r["kind"] != "tool":
            continue
        noncpu = r["action_s"] - (r["cpu_s"] or 0)
        covpct = 100.0 * r["cov_s"] / r["action_s"] if r["cov_s"] else float("nan")
        print(
            f"{r['kind']:<7}{r['name']:<12}{r['dur_s']:>8.3f}{r['action_s']:>9.3f}"
            f"{(r['cpu_s'] or 0):>8.3f}{noncpu:>9.3f}"
            f"{(r['util_pct'] or 0):>7.1f}{covpct:>7.1f}  {r['qual']}/{r['attrib']}"
        )

    print("\n=== Per tool-name summary (action vs cpu vs noncpu) ===")
    byname = defaultdict(list)
    for r in rows:
        if r["kind"] == "tool":
            byname[r["name"]].append(r)
    for k, lst in byname.items():
        n = len(lst)
        act = sum(x["action_s"] for x in lst)
        cpu = sum(x["cpu_s"] or 0 for x in lst)
        print(
            f"{k:<10} n={n:<3} total_action={act:>7.2f}s total_cpu={cpu:>7.2f}s "
            f"noncpu={act-cpu:>7.2f}s  avg_action={act/n:>6.3f}s avg_cpu={cpu/n:>6.3f}s"
        )


if __name__ == "__main__":
    main(sys.argv[1])
