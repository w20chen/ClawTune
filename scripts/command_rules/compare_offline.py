"""Compare two existing offline runs on exactly paired observations, read-only."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path


def read(path: Path) -> bytes:
    if path.is_file():
        return path.read_bytes()
    return gzip.decompress(path.with_suffix(path.suffix + ".gz").read_bytes())


def compare(before: Path, after: Path) -> dict:
    reports = [json.loads(read(p / "report.json")) for p in (before, after)]
    assert reports[0]["split_sha256"] == reports[1]["split_sha256"], "different task splits"
    rows = [[json.loads(line) for line in read(p / "model-predictions.jsonl").splitlines()]
            for p in (before, after)]
    assert len(rows[0]) == len(rows[1]), "different evaluation rows"
    grouped = defaultdict(list)
    for old, new in zip(*rows):
        identity = ("model", "task", "benchmark", "repo", "target", "tool", "actual", "actual_bucket")
        assert all(old.get(k) == new.get(k) for k in identity), "unaligned evaluation rows"
        grouped[(old["model"], old["target"])].append((old, new))
    comparisons = []
    for (model, target), pairs in sorted(grouped.items()):
        shared = [(a, b) for a, b in pairs if a["actual"] is not None and a["p50"] is not None and b["p50"] is not None]
        buckets = bool(shared) and all("predicted_bucket" in p[i] and "actual_bucket" in p[i] for p in shared for i in (0, 1))
        row = {"model": model, "target": target, "eligible": len(pairs), "paired": len(shared)}
        for index, name in enumerate(("phase1", "main")):
            row[name] = {"available": sum(p[index]["p50"] is not None for p in pairs)}
            if shared:
                row[name].update(
                    mae=sum(abs(p[index]["p50"] - p[index]["actual"]) for p in shared) / len(shared),
                    bucket_accuracy=(sum(p[index]["predicted_bucket"] == p[index]["actual_bucket"] for p in shared) / len(shared)) if buckets else None,
                )
        if shared:
            tasks = defaultdict(lambda: [0, 0., 0.])
            for old, new in shared:
                task = tasks[old["task"]]
                task[0] += 1
                task[1] += abs(new["p50"] - new["actual"]) - abs(old["p50"] - old["actual"])
                if buckets:
                    task[2] += int(new["predicted_bucket"] == new["actual_bucket"]) - int(old["predicted_bucket"] == old["actual_bucket"])
            rng = random.Random(42)
            values = list(tasks.values())
            samples = []
            for _ in range(2000):
                selected = rng.choices(values, k=len(values))
                count = sum(v[0] for v in selected)
                samples.append((sum(v[1] for v in selected) / count, sum(v[2] for v in selected) / count))
            row["paired_tasks"] = len(values)
            row["task_bootstrap_delta_95_percent"] = {
                name: [sorted(v[i] for v in samples)[49], sorted(v[i] for v in samples)[1949]]
                for i, name in enumerate(("mae", "bucket_accuracy") if buckets else ("mae",))}
        comparisons.append(row)
    return {
        "split_sha256": reports[0]["split_sha256"], "train_tasks": reports[1]["train_tasks"],
        "test_tasks": reports[1]["test_tasks"], "test_updates": reports[1]["test_updates"],
        "command_parsing": reports[1].get("command_parsing"), "comparisons": comparisons,
        "artifact_sha256": {name: {file: hashlib.sha256(read(path / file)).hexdigest()
                                  for file in ("report.json", "split.json", "model-predictions.jsonl")}
                            for name, path in (("phase1", before), ("main", after))},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = compare(args.before, args.after)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
