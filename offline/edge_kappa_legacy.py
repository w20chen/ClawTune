"""Original Lattice shrinkage bucket baseline on the EdgeKappa task split."""

from __future__ import annotations

import argparse
import json
import math
import shlex
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from statistics import mean, median, stdev

from edge_kappa_kb.graph import FeatureGraph
from offline.runner import inventory, load_or_create_split
from offline.edge_kappa_trace import read_v5_events
from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB
from tool_time._lattice_vendor.nodes import _loo_mse_log, _compute_loso_risk
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances


def _observation(event) -> ClauseObservation:
    argv = tuple(shlex.split(event.source_command))
    repo = next(feature[5:] for feature in event.query.features if feature.startswith("repo="))
    outcome = event.outcome
    return ClauseObservation(repo, argv[0], argv, outcome.start_time,
                             outcome.end_time, latency_ms=outcome.duration_ms)


def _correct_subset_coverage(kb: LatticeTimeKB, events: list) -> None:
    """Keep old nodes and selector; rebuild every node's true subset statistics."""
    graph = FeatureGraph(set(kb._nodes))
    durations: dict[str, list[float]] = {key: [] for key in graph.signatures}
    signatures: dict[str, dict[frozenset[str], list[float]]] = {
        key: {} for key in graph.signatures}
    for event in events:
        milliseconds = event.outcome.duration_ms
        if milliseconds is None or milliseconds <= 0:
            continue
        seconds = milliseconds / 1000
        for key in graph.subsets(event.query.features):
            durations[key].append(seconds)
            signatures[key].setdefault(event.query.features, []).append(seconds)
    for key, features in graph.signatures.items():
        values = durations[key]
        if not values:
            raise ValueError("old node has no subset-covering observations")
        stats = kb._nodes[features]
        logs = [math.log1p(value) for value in values]
        std_log = stdev(logs) if len(logs) >= 2 else kb._global_log_std
        log_means = [mean([math.log1p(value) for value in group])
                     for group in signatures[key].values()]
        stats.durations = values
        stats.count = len(values)
        stats.mean_s = mean(values)
        stats.median_s = median(values)
        stats.geometric_mean_s = math.exp(mean(math.log(value) for value in values))
        stats.mean_log = mean(logs)
        stats.std_log = std_log
        stats.stderr_log = std_log / math.sqrt(len(values)) if len(values) >= 2 else kb._global_log_std
        stats.loo_mse_log = _loo_mse_log(logs)
        stats.signature_count = len(log_means)
        stats.signature_log_mean_var = stdev(log_means) ** 2 if len(log_means) >= 2 else 0.0
        stats.loso_risk = _compute_loso_risk(signatures[key])
    compute_shrinkage_variances(kb._nodes, kappa=0.5, global_log_var=kb._global_log_var)


def run(dataset: Path, output: Path, *, benchmark: str = "swe-rebench", seed: int = 42,
        edges: tuple[float, ...] = (100, 500, 2000, 10000),
        split_cache_dir: Path | None = None,
        corrected_coverage: bool = False,
        built_in_coverage: bool = False) -> dict:
    if corrected_coverage and built_in_coverage:
        raise ValueError("choose one coverage mode")
    if (not edges or any(not math.isfinite(edge) or edge <= 0 for edge in edges)
            or tuple(sorted(set(edges))) != tuple(edges)):
        raise ValueError("bucket edges must be positive, finite, strictly increasing")
    dataset = dataset.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(dataset) or dataset.is_relative_to(output):
        raise ValueError("output must be separate from read-only dataset")
    tasks = inventory(dataset, benchmark)
    from benchmarks.bootstrap import ROOT
    split = load_or_create_split(tasks, dataset, seed,
        split_cache_dir or ROOT / ".runtime" / "offline" / "splits")
    train = []
    for key in split["train"]:
        events, _ = read_v5_events(dataset, tasks[key], key)
        train.extend(events)
    if not train:
        raise ValueError("no training clause labels")
    kb = LatticeTimeKB.fit(
        (_observation(event) for event in train),
        subset_coverage=built_in_coverage,
    )
    kb.freeze()
    if corrected_coverage:
        _correct_subset_coverage(kb, train)
    rows = []
    for key in split["test"]:
        events, _ = read_v5_events(dataset, tasks[key], key)
        for event in events:
            clause = _observation(event)
            results = kb.predict_clauses(clause.repo, [{"bin": clause.bin,
                "argv": clause.argv}], clause.ts_start)
            prediction = next(item for item in results[0].predictions
                              if item.algorithm == "shrinkage") if results else None
            predicted = (bisect_right(edges, prediction.prediction_ms) if prediction
                         and prediction.prediction_ms is not None else None)
            rows.append({"task": key, "call_id": event.outcome.call_id,
                "clause_id": event.outcome.clause_id,
                "actual_bucket": bisect_right(edges, event.outcome.duration_ms),
                "predicted_bucket": predicted,
                "prediction_ms": prediction.prediction_ms if prediction else None,
                "unavailable_reason": prediction.unavailable_reason if prediction else "no_clause_prediction"})
    scored = [row for row in rows if row["predicted_bucket"] is not None]
    report = {"schema": "edge-kappa-legacy-bucket-baseline.v1",
        "split_sha256": split["assignment_sha256"], "algorithm": "shrinkage",
        "bucket_edges_ms": list(edges),
        "corrected_coverage": corrected_coverage,
        "built_in_coverage": built_in_coverage,
        "train_clauses": len(train), "test_clauses": len(rows),
        "predicted": len(scored), "coverage": len(scored) / len(rows) if rows else 0,
        "accuracy": sum(row["actual_bucket"] == row["predicted_bucket"]
                        for row in scored) / len(scored) if scored else None,
        "far_bucket_error": sum(abs(row["actual_bucket"] - row["predicted_bucket"]) >= 2
                                for row in scored) / len(scored) if scored else None,
        "unavailable": dict(Counter(row["unavailable_reason"] for row in rows
                                    if row["predicted_bucket"] is None))}
    output.mkdir(parents=True, exist_ok=False)
    (output / "predictions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows),
                                              encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--benchmark", default="swe-rebench")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split-cache-dir", type=Path)
    parser.add_argument("--bucket-edges-ms", type=float, nargs="+",
                        default=(100, 500, 2000, 10000))
    coverage = parser.add_mutually_exclusive_group()
    coverage.add_argument("--corrected-coverage", action="store_true")
    coverage.add_argument("--built-in-coverage", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.dataset, args.output, benchmark=args.benchmark,
                         seed=args.seed, split_cache_dir=args.split_cache_dir,
                         edges=tuple(args.bucket_edges_ms),
                         corrected_coverage=args.corrected_coverage,
                         built_in_coverage=args.built_in_coverage), indent=2))


if __name__ == "__main__":
    main()
