"""Evaluate the exported lattice on manifest-held-out tasks without updating it."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

from export_resource_lattice import eligible_observations
from tool_resource.sdk import _load_valid_artifact
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.resource_lattice import RESOURCE_TARGETS, resource_values


def evaluate(dataset: Path, snapshot: Path) -> dict:
    manifest = json.loads(snapshot.with_suffix(".manifest.json").read_text())
    raw = snapshot.read_bytes()
    canonical = (json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if hashlib.sha256(canonical).hexdigest() != manifest["snapshot_sha256"]:
        raise ValueError("snapshot does not match the split manifest")
    train, test = set(manifest["train_tasks"]), set(manifest["test_tasks"])
    if train & test:
        raise ValueError("train/test task overlap")
    kb = LatticeTimeKB.from_json_obj(json.loads(raw))
    start = time.perf_counter()
    kb.prepare()
    prepare_seconds = time.perf_counter() - start
    print(f"Prepared {kb.observation_count} observations in {prepare_seconds:.2f}s", flush=True)
    metrics = defaultdict(lambda: {"eligible": 0, "predicted": 0, "errors": [], "covered_p90": 0})
    cache = {}
    timings = []
    rejected = []
    sources = []
    for path in sorted(dataset.rglob("clause_telemetry.json")):
        task = path.parent.parent.name
        if task not in test:
            continue
        repo = re.sub(r"-\d+$", "", task).replace("__", "/", 1)
        try:
            artifact = _load_valid_artifact(path)
        except ValueError as exc:
            rejected.append(str(exc).replace(str(dataset), "<dataset>"))
            continue
        rows, errors = eligible_observations(repo, artifact)
        rejected.extend(errors)
        sources.append({"path": path.relative_to(dataset).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "eligible_clauses": len(rows)})
        for row in rows:
            key = (repo, row.argv)
            if key not in cache:
                clause = {"bin": row.bin, "argv": list(row.argv)}
                start = time.perf_counter()
                resource = kb.predict_resource_clauses(repo, [clause], 1e12)
                latency = kb.predict_clauses(repo, [clause], 1e12)
                timings.append((time.perf_counter() - start) * 1000)
                cache[key] = (resource, latency)
            resource, latency = cache[key]
            values = resource_values(row)
            for clause in resource:
                for prediction in clause["predictions"]:
                    target = prediction["target"]
                    if target not in values:
                        continue
                    m = metrics[(target, prediction["algorithm"])]
                    m["eligible"] += 1
                    if prediction["p50"] is not None:
                        m["predicted"] += 1
                        scale = RESOURCE_TARGETS[target][1]
                        m["errors"].append(abs(math.log1p(prediction["p50"] / scale) - math.log1p(values[target] / scale)))
                        m["covered_p90"] += values[target] <= prediction["p90"]
            for clause in latency:
                for prediction in clause.predictions:
                    m = metrics[("latency_ms", prediction.algorithm)]
                    m["eligible"] += 1
                    if prediction.prediction_ms is not None:
                        m["predicted"] += 1
                        m["errors"].append(abs(math.log1p(prediction.prediction_ms / 1000) - math.log1p(row.latency_ms / 1000)))
    results = []
    for (target, algorithm), m in sorted(metrics.items()):
        n = m["predicted"]
        results.append({"target": target, "algorithm": algorithm, "eligible": m["eligible"],
                        "predicted": n, "coverage": n / m["eligible"],
                        "mean_abs_log1p_error": statistics.mean(m["errors"]) if n else None,
                        "p90_empirical_coverage": (m["covered_p90"] / n if n and target != "latency_ms" else None)})
    return {"split": manifest["split"], "train_tasks": len(train), "test_tasks": len(test),
            "test_updates": 0, "snapshot_sha256": manifest["snapshot_sha256"],
            "prepare_seconds": prepare_seconds, "unique_queries": len(cache),
            "query_p50_ms": statistics.median(timings) if timings else None,
            "query_p95_ms": sorted(timings)[max(0, math.ceil(.95 * len(timings))-1)] if timings else None,
            "query_timing_scope": "resource and time, all three algorithms, uncached unique queries",
            "metrics": results, "sources": sources, "rejected": rejected}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, default=Path(".runtime/lattice-export/clause-lattice-time-kb.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/resource-lattice-evaluation.json"))
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.dataset.resolve()):
        raise ValueError("evaluation output must be outside the read-only dataset")
    result = evaluate(args.dataset, args.snapshot)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("train_tasks", "test_tasks", "unique_queries", "query_p95_ms")}))
