"""Compare frozen runs with identical parser rules and task assignments."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def quantile(values, probability):
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def metrics(rows):
    available = [r for r in rows if r["p50"] is not None]
    result = {"eligible": len(rows), "available": len(available),
              "coverage": len(available) / len(rows) if rows else None}
    if not available:
        return result
    errors = [abs(r["p50"] - r["actual"]) / 1000 for r in available]
    result.update(
        mae_s=statistics.mean(errors), median_ae_s=statistics.median(errors),
        p90_ae_s=quantile(errors, .9), p95_ae_s=quantile(errors, .95),
        log1p_mae=statistics.mean(abs(math.log1p(r["p50"] / 1000) - math.log1p(r["actual"] / 1000)) for r in available),
        bucket_accuracy=statistics.mean(r["predicted_bucket"] == r["actual_bucket"] for r in available),
        under_5x_count=sum(r["actual"] > 5 * r["p50"] for r in available),
        under_5x_rate=statistics.mean(r["actual"] > 5 * r["p50"] for r in available),
    )
    return result


def bootstrap(pairs):
    by_task = defaultdict(list)
    for a, b in pairs:
        by_task[a["task"]].append((abs(b["p50"] - b["actual"]) / 1000 - abs(a["p50"] - a["actual"]) / 1000,
                                   int(b["predicted_bucket"] == b["actual_bucket"]) - int(a["predicted_bucket"] == a["actual_bucket"])))
    values = [(len(rows), sum(r[0] for r in rows), sum(r[1] for r in rows)) for rows in by_task.values()]
    rng, results = random.Random(42), [[], []]
    for _ in range(2000):
        sample = rng.choices(values, k=len(values))
        n = sum(r[0] for r in sample)
        for i in range(2):
            results[i].append(sum(r[i + 1] for r in sample) / n)
    return {"tasks": len(values), "after_minus_before_95_percent": {
        key: [quantile(results[i], .025), quantile(results[i], .975)]
        for i, key in enumerate(("mae_s", "bucket_accuracy"))}}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compare(before, after, index_path, output):
    output.mkdir(parents=True, exist_ok=True)
    reports = [read(p / "report.json") for p in (before, after)]
    index = read(index_path)
    assert reports[0]["split_sha256"] == reports[1]["split_sha256"] == index["split_sha256"]
    assert reports[0]["command_parsing"] == reports[1]["command_parsing"]
    assert all(r["test_updates"] == 0 for r in reports)
    assert read(before / "split.json") == read(after / "split.json")
    raw = [[json.loads(line) for line in (p / "model-predictions.jsonl").read_text().splitlines()] for p in (before, after)]
    assert len(raw[0]) == len(raw[1])
    for a, b in zip(*raw, strict=True):
        for key in ("model", "task", "target", "actual", "tool", "repo"):
            assert a[key] == b[key], key
        if a["model"] != "lattice":
            assert a == b, ("control changed", a["model"])
    calls_by_task = defaultdict(list)
    for call in index["calls"]:
        calls_by_task[call["task"]].append(call)
    mapped = []
    for rows in raw:
        counters = Counter()
        mapping = {}
        for row in rows:
            if row["target"] != "duration_ms":
                continue
            key = row["task"], row["model"]
            call = calls_by_task[row["task"]][counters[key]]
            counters[key] += 1
            assert math.isclose(call["actual_ms"], row["actual"], rel_tol=1e-12)
            assert call["tool"] == row["tool"]
            mapping[(call["id"], row["model"])] = row
        mapped.append(mapping)
    calls = []
    for c in index["calls"]:
        a, b = [m[(c["id"], "lattice")] for m in mapped]
        row = {key: c[key] for key in ("id", "task", "call_index", "repo", "tool", "command", "actual_ms")}
        row.update(source_file=c["raw"]["file"], source_line=c["raw"]["line"], before=a, after=b,
                   original=c["models"]["lattice"]["main"])
        if a["p50"] is not None and b["p50"] is not None:
            row["delta_ae_s"] = (abs(b["p50"] - b["actual"]) - abs(a["p50"] - a["actual"])) / 1000
        calls.append(row)
    paired = [c for c in calls if c["before"]["p50"] is not None and c["after"]["p50"] is not None]
    missing = [c for c in calls if c["before"]["p50"] is not None and c["after"]["p50"] is None]
    summary = {
        "scope": "duration; parser-fixed baseline versus Lattice core guard plus exact-own preference",
        "split_sha256": reports[0]["split_sha256"], "train_tasks": reports[0]["train_tasks"],
        "test_tasks": reports[0]["test_tasks"], "test_updates": 0,
        "command_parsing": reports[0]["command_parsing"],
        "all": {name: metrics([c[name] for c in calls]) for name in ("before", "after")},
        "paired": {name: metrics([c[name] for c in paired]) for name in ("before", "after")},
        "paired_bootstrap": bootstrap([(c["before"], c["after"]) for c in paired]),
        "lost_coverage": {"count": len(missing), "before_metrics": metrics([c["before"] for c in missing]),
                          "ids": [c["id"] for c in missing]},
        "new_coverage": [c["id"] for c in calls if c["before"]["p50"] is None and c["after"]["p50"] is not None],
        "controls_unchanged": ["tool", "trie", "edge_kappa"],
        "controls_on_paired": {model: metrics([mapped[1][(c["id"], model)] for c in paired]) for model in ("tool", "trie", "edge_kappa")},
        "paired_by_duration": {},
    }
    for name, low, high in [("0-10s", 0, 10), ("10-60s", 10, 60), ("60-300s", 60, 300), ("300s+", 300, math.inf)]:
        subset = [c for c in paired if low <= c["actual_ms"] / 1000 < high]
        summary["paired_by_duration"][name] = {v: metrics([c[v] for c in subset]) for v in ("before", "after")}
    own = {c["id"]: c for c in calls}
    old30 = [own[key] for key in index["rankings"]["unique_lattice_edge"]]
    new30 = sorted((c for c in calls if c["after"]["p50"] is not None),
                   key=lambda c: abs(c["after"]["p50"] - c["actual_ms"]), reverse=True)[:30]
    summary["old30_changes"] = dict(Counter(
        ("already_unavailable" if c["before"]["p50"] is None else "lost_prediction") if c["after"]["p50"] is None
        else "new_prediction" if c["before"]["p50"] is None
        else "improved" if c["delta_ae_s"] < -1e-6 else "worse" if c["delta_ae_s"] > 1e-6 else "unchanged" for c in old30))
    for version, path in (("before", before), ("after", after)):
        dest = output / version
        dest.mkdir(exist_ok=True)
        for name in ("report.json", "split.json", "model-predictions.jsonl"):
            (dest / (name + ".gz")).write_bytes(gzip.compress((path / name).read_bytes(), mtime=0))
    hashes = {}
    for file in (before / "seed").glob("*.json"):
        if file.name == "manifest.json":
            continue
        a, b = file.read_bytes(), (after / "seed" / file.name).read_bytes()
        assert a == b, ("training payload differs", file.name)
        hashes[file.name] = hashlib.sha256(a).hexdigest()
    summary["identical_training_payload_sha256"] = hashes
    write_json(output / "summary.json", summary)
    (output / "calls.json.gz").write_bytes(gzip.compress(json.dumps(calls, ensure_ascii=False).encode(), mtime=0))
    write_json(output / "old30.json", old30)
    write_json(output / "new30.json", new30)
    write_json(output / "largest_regressions.json", sorted(paired, key=lambda c: c["delta_ae_s"], reverse=True)[:30])
    def table(rows):
        def seconds(value):
            return "不可预测" if value is None else f"{value / 1000:.3f}"
        return '<table><tr><th>案例</th><th>实际秒</th><th>修复前秒</th><th>修复后秒</th><th>命令</th></tr>' + ''.join(
            '<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in (
                c["id"], seconds(c["actual_ms"]), seconds(c["before"]["p50"]), seconds(c["after"]["p50"]), c["command"])) + '</tr>' for c in rows) + '</table>'
    document = '<!doctype html><meta charset="utf-8"><title>Lattice P0 时间预测对照</title><style>body{font:15px system-ui;max-width:1500px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px;text-align:left}td:last-child{max-width:600px;overflow-wrap:anywhere}pre{white-space:pre-wrap}h2{margin-top:36px}</style>'
    document += '<h1>Lattice P0 时间预测对照</h1><p>两组均使用修复后的同一套命令规则。515 个训练任务、116 个测试任务，测试不更新 KB。只比较时间预测；分桶正确不能代替秒级误差。LogMAE 使用 log1p(秒)。无法预测不计为零误差，覆盖率单独报告。原最坏30来自此前规则修复前的审计，表内“修复前”是本次规则已修的独立基线。</p>'
    document += '<p>这是原诊断测试集上的修复验证，不是新的独立泛化评估。共同可预测样本仅来自 PennyLane 和 SQLGlot。完整原始指标、覆盖变化及按任务重采样区间见下方详情。</p>'
    document += '<h2>共同可预测样本</h2><table><tr><th>指标</th><th>修复前</th><th>修复后</th></tr>'
    for label, key, percent in [('MAE（秒）', 'mae_s', False), ('P90 绝对误差（秒）', 'p90_ae_s', False),
                                ('P95 绝对误差（秒）', 'p95_ae_s', False), ('分桶准确率', 'bucket_accuracy', True),
                                ('实际超过预测 5 倍的比例', 'under_5x_rate', True)]:
        document += '<tr><td>' + label + '</td>' + ''.join(
            '<td>' + (f'{summary["paired"][v][key]:.2%}' if percent else f'{summary["paired"][v][key]:.3f}') + '</td>'
            for v in ('before', 'after')) + '</tr>'
    document += '</table><details><summary>完整指标与一致性校验</summary><pre>' + html.escape(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre></details>'
    document += '<h2>原最坏30条的变化</h2>' + table(old30)
    document += '<h2>修复后最坏30条</h2>' + table(new30)
    document += '<h2>共同样本中退步最大的30条</h2>' + table(sorted(paired, key=lambda c: c["delta_ae_s"], reverse=True)[:30])
    (output / "report.html").write_text(document, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before", "after", "index", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    compare(args.before, args.after, args.index, args.output)
