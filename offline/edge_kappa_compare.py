"""Task-paired comparison of two EdgeKappaKB prediction directories."""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path


def _rows(path: Path) -> dict[tuple[str, str, str], dict]:
    result = {}
    with (path / "predictions.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            key = (row["task"], row["call_id"], row["clause_id"])
            if key in result:
                raise ValueError(f"duplicate prediction identity: {key}")
            result[key] = row
    return result


def _values(row: dict) -> tuple[float, float, float]:
    probabilities = row["probabilities"]
    actual = row["actual_bucket"]
    return (-math.log(probabilities[actual]),
            sum((p - int(index == actual)) ** 2 for index, p in enumerate(probabilities)),
            float(row["predicted_bucket"] == actual))


def _interval(values: list[float]) -> list[float]:
    ordered = sorted(values)
    return [ordered[int(.025 * (len(ordered) - 1))],
            ordered[int(.975 * (len(ordered) - 1))]]


def compare(baseline: Path, candidate: Path, *, seed: int = 42,
            repetitions: int = 2000, allow_replay_difference: bool = False) -> dict:
    if repetitions < 100:
        raise ValueError("bootstrap repetitions must be at least 100")
    base_report = json.loads((baseline / "report.json").read_text(encoding="utf-8"))
    candidate_report = json.loads((candidate / "report.json").read_text(encoding="utf-8"))
    if (base_report["split_sha256"] != candidate_report["split_sha256"] or
            base_report["bucket_edges_ms"] != candidate_report["bucket_edges_ms"] or
            base_report.get("event_clock", "record-order") !=
            candidate_report.get("event_clock", "record-order") or
            (not allow_replay_difference and
             base_report["replay_seed"] != candidate_report["replay_seed"])):
        raise ValueError("split, buckets, and training replay order must match")
    first, second = _rows(baseline), _rows(candidate)
    if set(first) != set(second):
        raise ValueError("prediction row identities differ")
    by_task = defaultdict(list)
    for key in sorted(first):
        a, b = first[key], second[key]
        if a["actual_bucket"] != b["actual_bucket"]:
            raise ValueError("paired actual bucket mismatch")
        if a["probabilities"] is None or b["probabilities"] is None:
            continue
        by_task[key[0]].append(tuple(y - x for x, y in zip(_values(a), _values(b))))
    tasks = sorted(by_task)
    if not tasks:
        raise ValueError("no paired predictions")
    all_differences = [difference for task in tasks for difference in by_task[task]]
    point = [sum(row[index] for row in all_differences) / len(all_differences)
             for index in range(3)]
    rng = random.Random(seed)
    bootstrap = [[], [], []]
    for _ in range(repetitions):
        sampled = [rng.choice(tasks) for _ in tasks]
        denominator = sum(len(by_task[task]) for task in sampled)
        for index in range(3):
            bootstrap[index].append(sum(row[index] for task in sampled
                                        for row in by_task[task]) / denominator)
    return {"schema": "edge-kappa-paired-comparison.v1",
            "baseline": baseline.name, "candidate": candidate.name,
            "split_sha256": base_report["split_sha256"],
            "baseline_replay_seed": base_report["replay_seed"],
            "candidate_replay_seed": candidate_report["replay_seed"],
            "paired_tasks": len(tasks), "paired_predictions": len(all_differences),
            "baseline_coverage": base_report["metrics"]["coverage"],
            "candidate_coverage": candidate_report["metrics"]["coverage"],
            "difference": {name: {"estimate": point[index], "task_bootstrap_95": _interval(bootstrap[index])}
                for index, name in enumerate(("log_loss", "brier", "accuracy"))}}


def compare_buckets(baseline: Path, candidate: Path, *, seed: int = 42,
                    repetitions: int = 2000) -> dict:
    """Compare a point-prediction baseline on the common predicted clauses."""
    if repetitions < 100:
        raise ValueError("bootstrap repetitions must be at least 100")
    first, second = _rows(baseline), _rows(candidate)
    if set(first) != set(second):
        raise ValueError("prediction row identities differ")
    baseline_report = json.loads((baseline / "report.json").read_text(encoding="utf-8"))
    candidate_report = json.loads((candidate / "report.json").read_text(encoding="utf-8"))
    if baseline_report["split_sha256"] != candidate_report["split_sha256"]:
        raise ValueError("split mismatch")
    if (not isinstance(baseline_report.get("bucket_edges_ms"), list) or
            baseline_report["bucket_edges_ms"] != candidate_report.get("bucket_edges_ms")):
        raise ValueError("bucket edges missing or mismatch")
    by_task = defaultdict(list)
    baseline_correct = candidate_correct = 0
    baseline_far = candidate_far = 0
    for key in sorted(first):
        a, b = first[key], second[key]
        if a["actual_bucket"] != b["actual_bucket"]:
            raise ValueError("actual bucket mismatch")
        if a["predicted_bucket"] is None or b["predicted_bucket"] is None:
            continue
        actual = a["actual_bucket"]
        a_correct = int(a["predicted_bucket"] == actual)
        b_correct = int(b["predicted_bucket"] == actual)
        a_far = int(abs(a["predicted_bucket"] - actual) >= 2)
        b_far = int(abs(b["predicted_bucket"] - actual) >= 2)
        baseline_correct += a_correct
        candidate_correct += b_correct
        baseline_far += a_far
        candidate_far += b_far
        by_task[key[0]].append((b_correct - a_correct, b_far - a_far))
    tasks = sorted(by_task)
    count = sum(len(by_task[task]) for task in tasks)
    if not count:
        raise ValueError("no common predicted clauses")
    rng = random.Random(seed)
    boot = [[], []]
    for _ in range(repetitions):
        sampled = [rng.choice(tasks) for _ in tasks]
        denominator = sum(len(by_task[task]) for task in sampled)
        for index in range(2):
            boot[index].append(sum(row[index] for task in sampled
                                   for row in by_task[task]) / denominator)
    return {"schema": "edge-kappa-paired-buckets.v1",
        "baseline": baseline.name, "candidate": candidate.name,
        "split_sha256": baseline_report["split_sha256"],
        "paired_tasks": len(tasks), "paired_predictions": count,
        "baseline_accuracy": baseline_correct / count,
        "candidate_accuracy": candidate_correct / count,
        "baseline_far_bucket_error": baseline_far / count,
        "candidate_far_bucket_error": candidate_far / count,
        "difference": {name: {"estimate": sum(row[index] for task in tasks
                          for row in by_task[task]) / count,
                          "task_bootstrap_95": _interval(boot[index])}
                       for index, name in enumerate(("accuracy", "far_bucket_error"))}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repetitions", type=int, default=2000)
    parser.add_argument("--allow-replay-difference", action="store_true")
    parser.add_argument("--bucket-only", action="store_true")
    args = parser.parse_args()
    result = (compare_buckets(args.baseline, args.candidate,
                             seed=args.seed, repetitions=args.repetitions)
              if args.bucket_only else
              compare(args.baseline, args.candidate,
                      seed=args.seed, repetitions=args.repetitions,
                      allow_replay_difference=args.allow_replay_difference))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
