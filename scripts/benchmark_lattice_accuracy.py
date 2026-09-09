"""Held-out lattice accuracy with physical errors and a paired empirical baseline.

Never trains or tunes on test tasks. Outputs only beneath the specified report
directory, outside the input dataset. Predictions are clause-level.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.export_resource_lattice import eligible_observations
from tool_resource.sdk import _load_valid_artifact
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.resource_lattice import resource_values

ALGORITHMS = ("shrinkage", "loso", "max_cardinality")
TARGETS = {
    "latency_ms": ("Wall time", "s", 1000.0),
    "cpu_time_seconds": ("CPU time", "core-s", 1.0),
    "cpu_avg_cores": ("CPU average", "cores", 1.0),
    "cpu_peak_cores": ("CPU peak", "cores", 1.0),
    "memory_peak_rss_bytes": ("Memory RSS peak", "MiB", 1024.0**2),
}


def quantiles(values):
    ordered = sorted(values)
    return statistics.median(ordered), ordered[math.ceil(.9 * len(ordered)) - 1]


class Baseline:
    """Repo+binary median/p90 with >=5 samples, else binary, else global.

    Group granularity/support are fixed before evaluation, no parameter prefix.
    All groups are populated from the frozen training snapshot only.
    """
    def __init__(self, observations):
        groups = defaultdict(list)
        for row in observations:
            values = resource_values(row)
            if row.latency_ms is not None and row.latency_ms > 0:
                values["latency_ms"] = row.latency_ms
            for target, value in values.items():
                for key in ((target, row.repo, row.bin), (target, row.bin), (target,)):
                    groups[key].append(value)
        self.groups = {key: (*quantiles(values), len(values)) for key, values in groups.items()}

    def predict(self, repo, binary, target):
        for key in ((target, repo, binary), (target, binary), (target,)):
            result = self.groups.get(key)
            if result and (result[2] >= 5 or len(key) == 1):
                return result
        return None


def summarize(rows):
    valid = [row for row in rows if row["p50"] is not None]
    result = {"eligible": len(rows), "predicted": len(valid),
              "prediction_coverage": len(valid) / len(rows) if rows else None}
    if not valid:
        return result
    errors = [abs(row["p50"] - row["actual"]) for row in valid]
    total_actual = sum(row["actual"] for row in valid)
    positive = [row for row in valid if row["actual"] > 0]
    ratios = [max(row["p50"] / row["actual"], row["actual"] / row["p50"])
              if row["p50"] > 0 else math.inf for row in positive]
    task_errors = defaultdict(list)
    for row, error in zip(valid, errors):
        task_errors[row["task"]].append(error)
    result.update({
        "mae": statistics.mean(errors), "median_absolute_error": statistics.median(errors),
        "p90_absolute_error": quantiles(errors)[1],
        "wape": sum(errors) / total_actual if total_actual else None,
        "within_2x": sum(r <= 2 for r in ratios) / len(ratios) if ratios else None,
        "positive_actual_count": len(positive),
        "macro_task_mae": statistics.mean(statistics.mean(v) for v in task_errors.values()),
        "evaluated_tasks": len(task_errors),
        "actual_p50": statistics.median(row["actual"] for row in valid),
        "actual_p90": quantiles([row["actual"] for row in valid])[1],
    })
    paired = [row for row in valid if row["baseline_p50"] is not None]
    if paired:
        baseline_mae = statistics.mean(abs(row["baseline_p50"] - row["actual"]) for row in paired)
        lattice_mae = statistics.mean(abs(row["p50"] - row["actual"]) for row in paired)
        result.update({"paired_baseline_count": len(paired), "paired_baseline_mae": baseline_mae,
                       "mae_reduction_vs_baseline": 1 - lattice_mae / baseline_mae if baseline_mae else None})
    tails = [row for row in valid if row["p90"] is not None]
    if tails:
        def pinball(actual, predicted):
            error = actual - predicted
            return .9 * error if error >= 0 else -.1 * error
        loss = statistics.mean(pinball(row["actual"], row["p90"]) for row in tails)
        base_loss = statistics.mean(pinball(row["actual"], row["baseline_p90"]) for row in tails)
        result.update({"p90_coverage": sum(row["actual"] <= row["p90"] for row in tails) / len(tails),
                       "p90_pinball_loss": loss, "paired_baseline_p90_pinball_loss": base_loss,
                       "p90_loss_reduction_vs_baseline": 1 - loss / base_loss if base_loss else None,
                       "baseline_p90_coverage": sum(row["actual"] <= row["baseline_p90"] for row in tails) / len(tails)})
    return result


def run(dataset: Path, snapshot: Path, output: Path):
    dataset, output = dataset.resolve(), output.resolve()
    if output.is_relative_to(dataset):
        raise ValueError("output must be outside the read-only dataset")
    raw = json.loads(snapshot.read_text())
    manifest = json.loads(snapshot.with_suffix(".manifest.json").read_text())
    snapshot_hash = hashlib.sha256((json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
    if snapshot_hash != manifest["snapshot_sha256"]:
        raise ValueError("snapshot differs from split manifest")
    train, test = set(manifest["train_tasks"]), set(manifest["test_tasks"])
    if train & test or raw["pending"]:
        raise ValueError("overlapping split or non-frozen snapshot")
    kb = LatticeTimeKB.from_json_obj(raw)
    baseline = Baseline(kb._observations)
    started = time.perf_counter()
    kb.prepare()
    prepare_seconds = time.perf_counter() - started
    print(f"Prepared frozen training KB in {prepare_seconds:.2f}s", flush=True)
    cache, records, sources, rejected, timings = {}, [], [], [], []
    for path in sorted(dataset.rglob("clause_telemetry.json")):
        task = path.parent.parent.name
        if task not in test:
            continue
        repo = re.sub(r"-\d+$", "", task).replace("__", "/", 1)
        try:
            artifact = _load_valid_artifact(path)
            rows, errors = eligible_observations(repo, artifact)
        except ValueError as exc:
            rejected.append(str(exc).replace(str(dataset), "<dataset>"))
            continue
        rejected.extend(errors)
        sources.append({"path": path.relative_to(dataset).as_posix(), "clauses": len(rows),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for row_index, row in enumerate(rows):
            key = (repo, row.argv)
            if key not in cache:
                clause = {"bin": row.bin, "argv": row.argv}
                started = time.perf_counter()
                resource = kb.predict_resource_clauses(repo, [clause], 1e12)
                latency = kb.predict_clauses(repo, [clause], 1e12)
                timings.append(1000 * (time.perf_counter() - started))
                predictions = {(p["target"], p["algorithm"]): p for c in resource for p in c["predictions"]}
                predictions.update({("latency_ms", p.algorithm): {
                    "p50": p.prediction_ms, "p90": None, "evidence_count": p.evidence_count,
                    "exact_match": p.exact_match, "unavailable_reason": p.unavailable_reason,
                } for c in latency for p in c.predictions})
                cache[key] = predictions
            values = {**resource_values(row), "latency_ms": row.latency_ms}
            for target, actual in values.items():
                base = baseline.predict(repo, row.bin, target)
                scale = TARGETS[target][2]
                for algorithm in ALGORITHMS:
                    prediction = cache[key].get((target, algorithm), {})
                    records.append({
                        "task": task, "repo": repo, "source": sources[-1]["path"], "row": row_index,
                        "binary": row.bin,
                        "command_hash": hashlib.sha256(json.dumps(row.argv).encode()).hexdigest()[:16],
                        "target": target, "unit": TARGETS[target][1], "algorithm": algorithm,
                        "actual": actual / scale,
                        "p50": prediction["p50"] / scale if prediction.get("p50") is not None else None,
                        "p90": prediction["p90"] / scale if prediction.get("p90") is not None else None,
                        "baseline_p50": base[0] / scale if base else None,
                        "baseline_p90": base[1] / scale if base else None,
                        "evidence_count": prediction.get("evidence_count", 0),
                        "exact_match": prediction.get("exact_match"),
                        "unavailable_reason": prediction.get("unavailable_reason"),
                    })
        if len(sources) % 10 == 0:
            print(f"Evaluated {len(sources)} test artifacts / {len(cache)} unique commands", flush=True)
    # Verify inference never changed the actual committed training records.
    after = json.loads(json.dumps(kb.to_json_obj()))
    if after["observations"] != raw["observations"] or after["pending"]:
        raise AssertionError("evaluation modified training observations")
    metrics = []
    for target in TARGETS:
        for algorithm in ALGORITHMS:
            subset = [r for r in records if r["target"] == target and r["algorithm"] == algorithm]
            metrics.append({"target": target, "algorithm": algorithm, "unit": TARGETS[target][1], **summarize(subset)})
    # Fixed diagnostic strata; do not use them to tune a model on the test set.
    cutoffs = {"latency_ms": 1.0, "cpu_time_seconds": 1.0,
               "cpu_avg_cores": .8, "cpu_peak_cores": 1.0, "memory_peak_rss_bytes": 128.0}
    strata = [{"target": target, "algorithm": alg, "actual_at_least": cutoff,
               "unit": TARGETS[target][1], **summarize([r for r in records if r["target"] == target
                    and r["algorithm"] == alg and r["actual"] >= cutoff])}
              for target, cutoff in cutoffs.items() for alg in ALGORITHMS]
    report = {"split": manifest["split"], "seed": manifest["seed"], "train_tasks": len(train),
              "test_tasks": len(test), "test_tasks_with_eligible_rows": len({r["task"] for r in records}),
              "test_repositories": len({r["repo"] for r in records}), "test_updates": 0,
              "snapshot_sha256": snapshot_hash, "baseline": Baseline.__doc__,
              "prepare_seconds": prepare_seconds, "unique_queries": len(cache),
              "query_p50_ms": statistics.median(timings), "query_p95_ms": sorted(timings)[math.ceil(.95*len(timings))-1],
              "metrics": metrics, "larger_workload_strata": strata, "sources": sources, "rejected": rejected}
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    with (output / "predictions.jsonl").open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, allow_nan=False) + "\n")
    render(report, output)
    return report


def render(report, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8), layout="constrained")
    colors = ("#2864b4", "#14836c", "#b87920")
    metrics = {(m["target"], m["algorithm"]): m for m in report["metrics"]}
    x = np.arange(len(TARGETS))
    for i, (alg, color) in enumerate(zip(ALGORITHMS, colors)):
        axes[0].bar(x+(i-1)*.25, [1-metrics[(t,alg)]["mae_reduction_vs_baseline"] for t in TARGETS],
                    width=.24, label=alg, color=color)
    axes[0].axhline(1, color="#555", linestyle="--", linewidth=1)
    axes[0].set_xticks(x, [v[0].replace(" ", "\n") for v in TARGETS.values()])
    axes[0].set_ylabel("MAE / paired baseline MAE (lower is better)")
    axes[0].set_title("Point accuracy vs. training-only repo/binary baseline")
    axes[0].legend(fontsize=8)
    resource_targets = list(TARGETS)[1:]
    x = np.arange(len(resource_targets))
    for i, (alg, color) in enumerate(zip(ALGORITHMS, colors)):
        axes[1].bar(x+(i-1)*.25, [100*metrics[(t,alg)]["p90_coverage"] for t in resource_targets],
                    width=.24, label=alg, color=color)
    axes[1].axhline(90, color="#555", linestyle="--", linewidth=1)
    axes[1].set_ylim(0, 105)
    axes[1].set_xticks(x, [TARGETS[t][0].replace(" ", "\n") for t in resource_targets])
    axes[1].set_ylabel("Observed value <= predicted p90 (%)")
    axes[1].set_title("p90 calibration on predicted test clauses (target: 90%)")
    fig.suptitle(f"Lattice offline test: {report['test_tasks']} held-out tasks, no test updates", fontsize=14)
    fig.savefig(output / "accuracy.png", dpi=160)
    plt.close(fig)
    text = ["# Lattice held-out accuracy", "", f"Train tasks: {report['train_tasks']}; test tasks: {report['test_tasks']}; seed: {report['seed']}.",
            "", "Frozen within-repository task split. No threshold or model tuning on this test run.",
            "All errors use predicted test clauses; paired baseline uses exactly those same clauses.",
            "Within-2x excludes zero actual values. WAPE = sum absolute error / sum actual.",
            "p90 coverage should be assessed together with pinball loss, not maximized blindly.", "",
            "| Target | Algorithm | N predicted/eligible | MAE | Median AE | WAPE | Within 2x | MAE reduction vs baseline | p90 coverage |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for m in report["metrics"]:
        text.append(f"| {m['target']} ({m['unit']}) | {m['algorithm']} | {m['predicted']}/{m['eligible']} | {m['mae']:.4g} | {m['median_absolute_error']:.4g} | {m['wape']:.1%} | {m['within_2x']:.1%} | {m['mae_reduction_vs_baseline']:.1%} | " + (f"{m['p90_coverage']:.1%}" if "p90_coverage" in m else "N/A") + " |")
    text.extend(["", "## Larger realized workloads (shrinkage)", "",
                 "Post-hoc diagnostic subsets defined by actual resource use, not ex-ante scheduling classes.",
                 "Their conditional p90 coverage is not expected to equal the overall 90% target.", "",
                 "| Actual workload at least | Predicted/eligible | MAE | Within 2x |",
                 "|---|---:|---:|---:|"])
    for m in report["larger_workload_strata"]:
        if m["algorithm"] == "shrinkage" and m["predicted"]:
            text.append(f"| {m['target']} >= {m['actual_at_least']} {m['unit']} | {m['predicted']}/{m['eligible']} | {m['mae']:.4g} {m['unit']} | {m['within_2x']:.1%} |")
    text.extend(["", "## Tail loss vs paired baseline", "",
                 "Pinball loss penalizes underestimation more strongly at p90; lower is better.",
                 "Positive reduction means the lattice improves over the baseline on the same clauses.", "",
                 "| Resource | Algorithm | p90 pinball loss | Baseline loss | Loss reduction |",
                 "|---|---|---:|---:|---:|"])
    for m in report["metrics"]:
        if "p90_pinball_loss" in m:
            text.append(f"| {m['target']} ({m['unit']}) | {m['algorithm']} | {m['p90_pinball_loss']:.4g} | {m['paired_baseline_p90_pinball_loss']:.4g} | {m['p90_loss_reduction_vs_baseline']:.1%} |")
    text.extend(["", "Full physical-unit errors, task-macro MAE, tail pinball losses and larger-workload strata are in metrics.json.",
                 "Per-clause actual/predicted pairs are in predictions.jsonl; command contents are replaced by hashes.",
                 "RSS is sampled clause-lineage RSS, not whole-call/cgroup memory. CPU source quota is 8 cores.",
                 "Time currently exposes point predictions only; no time p90 is invented in this evaluation.",
                 "Singleton repos are train-only. Results measure within-repo generalization, not unseen-repo deployment."])
    (output / "report.md").write_text("\n".join(text) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "traces/tool-resource/clause-lattice-time-kb.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/lattice-accuracy")
    args = parser.parse_args()
    result = run(args.dataset, args.snapshot, args.output)
    print(json.dumps({k: result[k] for k in ("test_tasks", "test_repositories", "unique_queries", "query_p95_ms")}))
