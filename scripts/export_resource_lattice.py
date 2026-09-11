"""Reproducible within-repository task-held-out lattice seed from read-only eBPF artifacts.

Only this script's output files are written. Legacy timestamps are not causal;
the seed is explicitly a static training corpus, not a chronological evaluation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "sidecar" / "src"))

from tool_resource.sdk import _load_valid_artifact, _observation_from_clause
from tool_resource.features import enrich_clause_structure
from tool_resource.runtime_kb import ClauseResourceKB, is_pipeline_dependent_consumer
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.resource_lattice import resource_values


def eligible_observations(repo: str, artifact: dict) -> tuple[list, list[str]]:
    """Same target/boundary eligibility for train and held-out evaluation."""
    provenance = artifact.get("provenance", {})
    observations, errors = [], []
    for call in artifact["calls"]:
        if call.get("eligible_for_kb") is not True or call.get("telemetry_quality") != "ok":
            continue
        rows = enrich_clause_structure(call.get("command"), call.get("clauses", []))
        for row in rows:
            availability = row.get("availability", {})
            if (row.get("eligible_for_kb") is not True
                or row.get("telemetry_quality") != "ok"
                or availability.get("latency") != "ok"):
                continue
            boundary = row.get("provenance", {}).get("boundary_coverage", {})
            if not boundary.get("has_exec") or not boundary.get("has_exit"):
                continue
            row_provenance = row.get("provenance", {})
            if row_provenance.get("exit_signal") or row_provenance.get("exit_signals"):
                continue
            try:
                observation = _observation_from_clause(repo, row, require_timestamps=False)
            except (ValueError, TypeError) as exc:
                errors.append(f"clause:{exc}")
                continue
            observation = replace(
                observation,
                peak_cpu_cores=(observation.peak_cpu_cores if availability.get("cpu") == "ok"
                                and provenance.get("window_ns") == 500_000_000 else None),
                sampled_peak_rss_mb=(observation.sampled_peak_rss_mb
                                    if availability.get("memory") == "ok" else None),
            )
            if observation.latency_ms is None or observation.latency_ms <= 0:
                continue
            if is_pipeline_dependent_consumer(observation):
                continue
            observations.append(observation)
    return observations, errors


def export(
    dataset: Path,
    output: Path,
    *,
    seed: int = 42,
    train_fraction: float = 0.8,
    clause_output: Path | None = None,
) -> dict:
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    dataset = dataset.resolve()
    output = output.resolve()
    if output.is_relative_to(dataset):
        raise ValueError("output must not be inside the read-only dataset")
    if clause_output is not None:
        clause_output = clause_output.resolve()
        if clause_output.is_relative_to(dataset):
            raise ValueError("clause output must not be inside the read-only dataset")
    files = sorted(dataset.rglob("clause_telemetry.json"))
    if not files:
        raise ValueError("no clause_telemetry.json artifacts found")
    groups = {}
    for path in files:
        task = path.parent.parent.name
        repo = re.sub(r"-\d+$", "", task).replace("__", "/", 1)
        groups.setdefault(repo, {}).setdefault(task, []).append(path)
    train_tasks, test_tasks = [], []
    train_files = []
    for repo, tasks in sorted(groups.items()):
        names = sorted(tasks)
        random.Random(f"{seed}:{repo}").shuffle(names)
        count = max(1, min(len(names) - 1, int(len(names) * train_fraction))) if len(names) > 1 else 1
        train_tasks.extend(names[:count])
        test_tasks.extend(names[count:])
        train_files.extend((repo, path) for task in names[:count] for path in tasks[task])
    observations = []
    rejected = []
    sources = []
    counts = Counter()
    quotas = set()
    for repo, path in sorted(train_files):
        relative = path.relative_to(dataset).as_posix()
        try:
            artifact = _load_valid_artifact(path)
        except ValueError as exc:
            rejected.append({"path": relative, "reason": str(exc).replace(str(dataset), "<dataset>")})
            continue
        if artifact.get("quota_cores") is not None:
            quotas.add(artifact["quota_cores"])
        rows, errors = eligible_observations(repo, artifact)
        observations.extend(rows)
        rejected.extend({"path": relative, "reason": error} for error in errors)
        for row in rows:
            counts.update(resource_values(row).keys())
        added = len(rows)
        sources.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "observations": added})
    if not observations:
        raise ValueError("training split has no eligible observations")
    kb = LatticeTimeKB.fit(observations)
    payload = kb.to_json_obj()
    output.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    output.write_bytes(data)
    clause_data = None
    if clause_output is not None:
        clause_payload = ClauseResourceKB.fit_public(observations).to_json_obj()
        clause_data = (
            json.dumps(
                clause_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            + "\n"
        ).encode()
        clause_output.parent.mkdir(parents=True, exist_ok=True)
        clause_output.write_bytes(clause_data)
    manifest = {
        "schema": "resource_lattice_seed_manifest_v1", "seed": seed,
        "split": "task_holdout_within_repository", "train_fraction": train_fraction,
        "train_tasks": sorted(train_tasks),
        "test_tasks": sorted(test_tasks),
        "singleton_repositories_train_only": sorted(repo for repo, tasks in groups.items() if len(tasks) == 1),
        "observation_count": kb.observation_count, "target_counts": dict(sorted(counts.items())),
        "scope": "clause_owned_lineage", "memory_metric": "sampled_distinct_mm_rss",
        "cpu_peak_window_ms": 500, "source_quota_cores": sorted(quotas),
        "environment_limit": "Empirical predictions are conditional on source execution conditions; not unconstrained CPU demand.",
        "timestamp_policy": "Static training; missing clause timestamps use 0/latency and cannot support chronological replay.",
        "snapshot_sha256": hashlib.sha256(data).hexdigest(),
        "sources": sources, "rejected": rejected,
    }
    if clause_data is not None:
        manifest["clause_snapshot_sha256"] = hashlib.sha256(clause_data).hexdigest()
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/lattice-export/clause-lattice-time-kb.json")
    parser.add_argument("--clause-output", type=Path, default=ROOT / ".runtime/lattice-export/clause-resource-kb.json")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-fraction", type=float, default=0.8)
    args = parser.parse_args()
    result = export(
        args.dataset,
        args.output,
        seed=args.seed,
        train_fraction=args.train_fraction,
        clause_output=args.clause_output,
    )
    print(json.dumps({key: result[key] for key in ("observation_count", "target_counts", "snapshot_sha256")}))
