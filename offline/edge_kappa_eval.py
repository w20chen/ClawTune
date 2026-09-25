"""Frozen, clause-level EdgeKappaKB study on the existing fixed task split."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shlex
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from edge_kappa_kb import EdgeKappaKB, TimeOutcome, TrainingEvent
from offline.runner import inventory, load_or_create_split, load_task
from tool_resource.runtime_kb import is_pipeline_dependent_consumer
from tool_resource.features import shell_bin_requires_exec_evidence
from tool_time.edge_kappa_adapter import shell_query


def _events(dataset: Path, tasks: dict, keys: list[str], rss_unit: str,
            event_clock: str) -> tuple[list[TrainingEvent], Counter]:
    events = []
    exclusions = Counter()
    for key in keys:
        if event_clock == "trace":
            from offline.edge_kappa_trace import read_v5_events
            loaded_events, excluded = read_v5_events(dataset, tasks[key], key)
            events.extend(loaded_events)
            exclusions.update(excluded)
            continue
        loaded = load_task(dataset, tasks[key], rss_unit, recorded_clauses_only=True)
        for index, clause in enumerate(loaded.clauses):
            if clause.latency_ms is None or not math.isfinite(clause.latency_ms) or clause.latency_ms < 0:
                exclusions["no_exact_clause_duration"] += 1
                continue
            if not clause.argv or not clause.bin:
                exclusions["invalid_clause"] += 1
                continue
            if is_pipeline_dependent_consumer(clause) or not shell_bin_requires_exec_evidence(
                clause.bin, clause.argv[0]):
                exclusions["unsupported_shell_clause"] += 1
                continue
            command = shlex.join(clause.argv)
            try:
                query = shell_query(command, repo=clause.repo)
            except ValueError:
                exclusions["normalization_failed"] += 1
                continue
            # The common loader keeps trace order but zeroes the timestamps.
            # This order is suitable for deterministic batch replay only.
            start = float(index * 2)
            identity = f"{key}:{index}"
            outcome = TimeOutcome(identity, start, start + 1, key, identity, "0",
                                  duration_ms=float(clause.latency_ms),
                                  label_source="clause")
            events.append(TrainingEvent(query, outcome, command))
    return events, exclusions


def _score(rows: list[dict], bucket_count: int) -> dict:
    labeled = [row for row in rows if row["actual_bucket"] is not None]
    scored = [row for row in labeled if row["probabilities"] is not None]
    if not scored:
        return {"labeled": len(labeled), "predicted": 0, "coverage": 0.0,
                "log_loss": None, "brier": None, "accuracy": None,
                "macro_recall": None, "far_bucket_error": None}
    loss = sum(-math.log(row["probabilities"][row["actual_bucket"]]) for row in scored)
    brier = sum(sum((probability - int(index == row["actual_bucket"])) ** 2
                    for index, probability in enumerate(row["probabilities"]))
                for row in scored)
    accuracy = sum(row["predicted_bucket"] == row["actual_bucket"] for row in scored)
    far = sum(abs(row["predicted_bucket"] - row["actual_bucket"]) >= 2 for row in scored)
    recall = []
    for bucket in range(bucket_count):
        subset = [row for row in scored if row["actual_bucket"] == bucket]
        if subset:
            recall.append(sum(row["predicted_bucket"] == bucket for row in subset) / len(subset))
    return {"labeled": len(labeled), "predicted": len(scored),
            "coverage": len(scored) / len(labeled) if labeled else 0.0,
            "log_loss": loss / len(scored), "brier": brier / len(scored),
            "accuracy": accuracy / len(scored),
            "macro_recall": sum(recall) / len(recall), "far_bucket_error": far / len(scored)}


def run(dataset: Path, output: Path, *, benchmark: str | None = None, seed: int = 42,
        k0: float = 1.0, learning_rate: float = 0.1, learn_weights: bool = True,
        shared_child_weight: bool = False,
        online: bool = False,
        replay_seed: int = 42,
        event_clock: str = "record-order",
        rss_unit: str = "MiB", split_cache_dir: Path | None = None,
        train_fraction: float = .8, bucket_edges_ms: tuple[float, ...] = (100, 500, 2000, 10000)) -> dict:
    dataset = dataset.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(dataset) or dataset.is_relative_to(output):
        raise ValueError("output must be separate from the read-only dataset")
    if event_clock not in {"record-order", "trace"}:
        raise ValueError("unsupported event clock")
    excluded_files: list[dict] = []
    tasks = inventory(dataset, benchmark, excluded_files)
    names = {task["benchmark"] for task in tasks.values()}
    if len(names) != 1:
        raise ValueError("select exactly one benchmark to avoid cross-suite training")
    from benchmarks.bootstrap import ROOT
    split = load_or_create_split(tasks, dataset, seed,
        split_cache_dir or ROOT / ".runtime" / "offline" / "splits", train_fraction)
    if not split["test"]:
        raise ValueError("split has no test tasks")
    training, train_exclusions = _events(dataset, tasks, split["train"], rss_unit, event_clock)
    test_order = sorted(split["test"], key=lambda key: hashlib.sha256(key.encode()).hexdigest())
    validation, test_exclusions = _events(dataset, tasks, test_order, rss_unit, event_clock)
    if event_clock == "trace":
        task_position = {task: index for index, task in enumerate(test_order)}
        validation.sort(key=lambda event: (
            task_position[event.outcome.task_id], event.outcome.start_time,
            event.outcome.event_id,
        ))
    if not training:
        raise ValueError("training split has no trusted clause duration labels")
    kb = EdgeKappaKB.fit(training, {"bucket_edges_ms": bucket_edges_ms,
        "k0": k0, "learning_rate": learning_rate, "learn_weights": learn_weights,
        "shared_child_weight": shared_child_weight, "replay_seed": replay_seed})
    kb.freeze()
    frozen = kb.to_snapshot()
    model = kb.fork_for_update() if online else kb
    rows = []
    previous_task: str | None = None
    for event in validation:
        if online and previous_task is not None and event.outcome.task_id != previous_task:
            model.commit_before(float("inf"))
        prediction = (model.begin(event.query, event.outcome.event_id,
                                  event.outcome.start_time, task_id=event.outcome.task_id,
                                  call_id=event.outcome.call_id,
                                  clause_id=event.outcome.clause_id)[0] if online
                      else model.predict(event.query))
        actual = bisect_right(bucket_edges_ms, event.outcome.duration_ms)
        row = {"task": event.outcome.task_id, "repo": tasks[event.outcome.task_id]["group"],
            "call_id": event.outcome.call_id, "clause_id": event.outcome.clause_id,
            "command": event.source_command,
            "command_features": sorted(event.query.features),
            "actual_bucket": actual, "predicted_bucket": prediction.bucket,
            "probabilities": prediction.probabilities, "unavailable_reason": prediction.unavailable_reason,
            "node_id": prediction.node_id, "exact_match": prediction.exact_match,
            "node_observations": prediction.observation_count,
            "evidence_union_count": prediction.evidence_union_count,
            "evidence_overlap_ratio": prediction.evidence_overlap_ratio,
            "parents": [asdict(item) for item in prediction.evidence],
            "generation": model.generation}
        rows.append(row)
        if online:
            model.complete(event.outcome.event_id, event.outcome)
        previous_task = event.outcome.task_id
    if online:
        model.commit_before(float("inf"))
    if kb.to_snapshot() != frozen:
        raise RuntimeError("frozen validation mutated the KB")
    by_repo: dict[str, list[dict]] = defaultdict(list)
    by_history: dict[str, list[dict]] = defaultdict(list)
    by_signature: dict[str, list[dict]] = defaultdict(list)
    by_sqlglot: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)
        n = row["node_observations"]
        history = "0" if n == 0 else "1-9" if n < 10 else "10-49" if n < 50 else "50+"
        by_history[history].append(row)
        by_signature["exact" if row["exact_match"] else "temporary"].append(row)
        by_sqlglot["sqlglot" if "sqlglot" in row["repo"].lower() else "other"].append(row)
    worst = sorted((row for row in rows if row["probabilities"] is not None),
                   key=lambda row: -math.log(row["probabilities"][row["actual_bucket"]]),
                   reverse=True)[:20]
    report = {"schema": "edge-kappa-eval.v1", "benchmark": next(iter(names)),
        "mode": "trace_online" if online and event_clock == "trace" else
                "record_order_online" if online else "frozen",
        "event_clock": event_clock,
        "test_updates": model.generation - kb.generation,
        "split_sha256": split["assignment_sha256"], "seed": seed,
        "replay_seed": replay_seed,
        "train_tasks": len(split["train"]), "test_tasks": len(split["test"]),
        "train_clauses": len(training), "test_clauses": len(validation),
        "graph_nodes": len(kb.graph.signatures), "graph_edges": len(kb.edges),
        "final_graph_nodes": len(model.graph.signatures),
        "final_graph_edges": len(model.edges),
        "effective_max_optional_features": kb.max_optional_features,
        "bucket_edges_ms": list(bucket_edges_ms), "k0": k0,
        "learning_rate": learning_rate, "learn_weights": learn_weights,
        "shared_child_weight": shared_child_weight,
        "replay_order": ("task hash order; within each task original clause start/end timestamps"
                         if event_clock == "trace" else
                         "task hash order; within each task trace clause record order; source timestamps unavailable in common loader"),
        "metrics": _score(rows, len(bucket_edges_ms) + 1),
        "repositories": {repo: _score(selected, len(bucket_edges_ms) + 1)
                         for repo, selected in sorted(by_repo.items())},
        "history": {name: _score(selected, len(bucket_edges_ms) + 1)
                    for name, selected in sorted(by_history.items())},
        "signatures": {name: _score(selected, len(bucket_edges_ms) + 1)
                       for name, selected in sorted(by_signature.items())},
        "workloads": {name: _score(selected, len(bucket_edges_ms) + 1)
                      for name, selected in sorted(by_sqlglot.items())},
        "worst_cases": [{"task": row["task"], "call_id": row["call_id"],
            "actual_bucket": row["actual_bucket"], "predicted_bucket": row["predicted_bucket"],
            "log_loss": -math.log(row["probabilities"][row["actual_bucket"]])}
            for row in worst],
        "excluded_files": excluded_files, "excluded_train_clauses": dict(train_exclusions),
        "excluded_test_clauses": dict(test_exclusions)}
    output.mkdir(parents=True, exist_ok=False)
    (output / "split.json").write_text(json.dumps(split, indent=2) + "\n", encoding="utf-8")
    (output / "edge-kappa-kb.json").write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    (output / "predictions.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n"
                                              for row in rows), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                                        encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--replay-seed", type=int, default=42)
    parser.add_argument("--k0", type=float, default=1.0)
    parser.add_argument("--learning-rate", type=float, default=0.1)
    weight_mode = parser.add_mutually_exclusive_group()
    weight_mode.add_argument("--fixed-weights", action="store_true")
    weight_mode.add_argument("--shared-child-weight", action="store_true")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--event-clock", choices=("record-order", "trace"), default="record-order")
    parser.add_argument("--rss-unit", choices=("MB", "MiB"), default="MiB")
    parser.add_argument("--split-cache-dir", type=Path)
    parser.add_argument("--train-fraction", type=float, default=.8)
    parser.add_argument("--bucket-edges-ms", type=float, nargs="+", default=(100, 500, 2000, 10000))
    args = parser.parse_args()
    report = run(args.dataset, args.output, benchmark=args.benchmark, seed=args.seed,
                 k0=args.k0, learning_rate=args.learning_rate,
                 learn_weights=not args.fixed_weights,
                 shared_child_weight=args.shared_child_weight, online=args.online,
                 replay_seed=args.replay_seed,
                 event_clock=args.event_clock,
                 rss_unit=args.rss_unit,
                 split_cache_dir=args.split_cache_dir, train_fraction=args.train_fraction,
                 bucket_edges_ms=tuple(args.bucket_edges_ms))
    print(json.dumps(report["metrics"], indent=2))


if __name__ == "__main__":
    main()
