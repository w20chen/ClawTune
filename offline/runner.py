from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from benchmarks.bootstrap import ROOT
from benchmarks.adapters import NAMES
from clawtune_kb import FILES, create_seed
from clawtune_kb.store import digest, write_json
from cold_start.flat_loader import read_task, LoadedTask
from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB, ToolCallQuery, _target_values
from tool_time.lattice_kb import LatticeTimeKB


def canonical_benchmark(name: str) -> str:
    if name.startswith("bfcl-"):
        return "bfcl"
    return {"deepresearchbench": "deep-research-bench", "terminalbench": "terminal-bench"}.get(name, name)


def inventory(dataset: Path, benchmark: str | None, excluded: list | None = None) -> dict:
    result = {}
    excluded = excluded if excluded is not None else []
    for path in sorted(dataset.rglob("*")):
        if not path.is_file() or path.suffix not in {".jsonl", ".trace"}:
            continue
        if not path.resolve().is_relative_to(dataset):
            raise ValueError("trace resolves outside the read-only dataset")
        if path.name != "trace.jsonl" and (path.parent / "trace.jsonl").is_file():
            excluded.append({"path": path.relative_to(dataset).as_posix(), "reason": "canonical trace.jsonl exists in this attempt"})
            continue
        metadata = None
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if '"trace_metadata"' not in line:
                    continue
                row = json.loads(line)
                if row.get("type", row.get("record_type")) == "trace_metadata":
                    metadata = row
                    break
        if metadata is None:
            continue
        if metadata.get("source_trace_count", 0) > 1 and not metadata.get("instance_id"):
            excluded.append({"path": path.relative_to(dataset).as_posix(), "reason": "aggregate simulation; use per-task traces"})
            continue
        side = path.parent / "dataset-task.json"
        if side.exists():
            metadata = {**metadata, **json.loads(side.read_text(encoding="utf-8"))}
        name = canonical_benchmark(str(metadata.get("benchmark") or benchmark or ""))
        if name not in NAMES:
            raise ValueError(f"{path}: missing or unsupported benchmark; supply --benchmark")
        if benchmark and name != benchmark:
            continue
        task_id = metadata.get("instance_id") or metadata.get("task_id")
        if not task_id:
            raise ValueError(f"{path}: trace requires task identity metadata or dataset-task.json")
        task_id = str(task_id)
        repo = metadata.get("repo")
        if not repo and name in {"swe-rebench", "swe-bench-verified"}:
            from swe_rebench.task_source import infer_repo_from_instance_id
            repo = infer_repo_from_instance_id(task_id)
            if not repo:
                raise ValueError(f"{path}: repository cannot be determined")
        category = metadata.get("category")
        if not category and str(metadata.get("benchmark", "")).startswith("bfcl-"):
            category = str(metadata["benchmark"])[5:]
        group = str(repo or category or "dataset")
        key = f"{name}:{task_id}"
        entry = {"benchmark": name, "task_id": task_id, "group": group,
                 "grouping": "repository" if repo else "category" if category else "dataset",
                 "files": []}
        if key in result and any(result[key][field] != entry[field] for field in ("group", "grouping")):
            raise ValueError(f"inconsistent task identity: {key}")
        stored = result.setdefault(key, entry)
        version = metadata.get("trace_format_version", metadata.get("schema_version"))
        if version not in {5, 6}:
            raise ValueError(f"{path}: only trace v5/v6 are supported")
        stored["files"].append({"path": path.relative_to(dataset).as_posix(), "sha256": digest(path), "version": version})
    if not result:
        raise ValueError("no fixed-format task traces found")
    return result


def split_tasks(tasks: dict, seed: int) -> dict:
    groups = defaultdict(list)
    for key, task in tasks.items():
        groups[(task["benchmark"], task["group"])].append(key)
    train, test, counts = [], [], []
    for (benchmark, group), members in sorted(groups.items()):
        members.sort(key=lambda key: hashlib.sha256(json.dumps([seed, benchmark, group, key]).encode()).hexdigest())
        n = max(1, math.floor(.8 * len(members)))
        train.extend(members[:n])
        test.extend(members[n:])
        counts.append({"benchmark": benchmark, "group": group, "train": n, "test": len(members) - n})
    return {"schema": "clawtune.split.v1", "seed": seed, "train_fraction": .8,
            "rule": "sha256-per-benchmark-group-task-v1", "tasks": tasks,
            "train": sorted(train), "test": sorted(test), "groups": counts}


def load_task(dataset: Path, task: dict, rss_unit: str) -> LoadedTask:
    result = LoadedTask()
    seen = set()
    namespace = task["benchmark"] + ":" + task["group"]
    for file in task["files"]:
        path = dataset / file["path"]
        if digest(path) != file["sha256"]:
            raise ValueError(f"dataset changed: {file['path']}")
        if file["sha256"] in seen:
            continue
        seen.add(file["sha256"])
        if file["version"] == 5:
            loaded = read_task(path, repo=namespace, task_id=task["task_id"], rss_unit=rss_unit)
        else:
            from clawtune_sidecar.predictors.tool_resource import load_openclaw_trace_observations
            loaded_v6 = load_openclaw_trace_observations(path, repo=namespace)
            # V6 import uses the same ownership/quality gates as runtime import.
            loaded = LoadedTask(calls=[replace(call, repo=namespace) for call in loaded_v6.completed_calls])
            from tool_resource.sdk import _observations_from_call
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    if row.get("record_type") != "span_end" or row.get("kind") != "tool":
                        continue
                    telemetry = (row.get("execution") or {}).get("tool_resource") or {}
                    call = telemetry.get("call_telemetry") or {}
                    if call.get("eligible_for_kb") is True and call.get("telemetry_quality") == "ok":
                        loaded.clauses.extend(_observations_from_call(namespace, call, require_timestamps=False))
        # Static train/test has no chronological replay semantics.
        result.calls.extend(replace(call, ts_start=0., ts_end=call.ts_end - call.ts_start) for call in loaded.calls)
        result.clauses.extend(replace(row, ts_start=0., ts_end=row.ts_end - row.ts_start) for row in loaded.clauses)
        result.counts.update(loaded.counts)
    return result


def run(dataset: Path, output: Path, *, benchmark: str | None = None, seed: int = 42, rss_unit: str = "MiB") -> dict:
    from clawtune_kb.contracts import validate
    dataset, output = dataset.resolve(strict=True), output.resolve()
    if output.is_relative_to(dataset) or dataset.is_relative_to(output):
        raise ValueError("experiment output and read-only dataset must be separate directories")
    excluded = []
    tasks = inventory(dataset, benchmark, excluded)
    dataset_names = sorted({task["benchmark"] for task in tasks.values()})
    if len(dataset_names) > 1:
        # Never share global fallback training between overlapping suites.
        output.mkdir(parents=True, exist_ok=False)
        reports = {name: run(dataset, output / name, benchmark=name, seed=seed, rss_unit=rss_unit)
                   for name in dataset_names}
        report = {"schema": "clawtune.offline-report.v1", "train_tasks": sum(r["train_tasks"] for r in reports.values()),
                  "test_tasks": sum(r["test_tasks"] for r in reports.values()), "test_updates": 0,
                  "datasets": reports, "metrics": [m for r in reports.values() for m in r["metrics"]]}
        validate(report, "offline-report.schema.json")
        write_json(output / "report.json", report)
        (output / "report.md").write_text("# Independent dataset evaluations\n\n" + "\n".join(f"- [{name}]({name}/report.md)" for name in dataset_names) + "\n", encoding="utf-8")
        return report
    manifest = split_tasks(tasks, seed)
    manifest["excluded_files"] = excluded
    validate(manifest, "offline-split.schema.json")
    if not manifest["test"]:
        raise ValueError("no test tasks: every group is a singleton")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "split.json", manifest)
    clauses, calls, counts = [], [], Counter()
    for key in manifest["train"]:
        loaded = load_task(dataset, tasks[key], rss_unit)
        clauses.extend(loaded.clauses)
        calls.extend(loaded.calls)
        counts.update(loaded.counts)
    trie = ClauseResourceKB.fit_public(clauses) if clauses else ClauseResourceKB()
    runtime = RuntimeToolResourceKB.fit_public(calls) if calls else RuntimeToolResourceKB()
    for row in clauses:
        trie.observe_completed_clause(row)
    for call in calls:
        runtime.observe_completed_call(call)
    if not any(not call.censored for call in calls) and not clauses:
        raise ValueError("training split contains no usable completed observations")
    trie._absorb_completed(float("inf"))
    runtime._absorb_completed(float("inf"))
    lattice = LatticeTimeKB.fit(clauses)
    bundle = create_seed(output / "seed", dict(zip(FILES, (trie.to_json_obj(), runtime.to_json_obj(), lattice.to_json_obj()))),
        provenance={"split_sha256": digest(output / "split.json"), "train": manifest["train"],
                    "test": manifest["test"], "rss_unit": rss_unit, "counts": dict(counts)})
    trie.freeze()
    runtime.freeze()
    lattice.freeze()
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.prediction_config import load_bucket_edges
    edges = load_bucket_edges((100, 500, 2000, 10000))
    baselines = defaultdict(list)
    for call in calls:
        if not call.censored:
            for target, value in _target_values(call).items():
                baselines[(call.tool_name, "duration_ms" if target == "latency_ms" else target)].append(value)
    metrics, rows = defaultdict(list), []
    for key in manifest["test"]:
        task = tasks[key]
        loaded = load_task(dataset, task, rss_unit)
        for call in loaded.calls:
            if call.censored:
                continue
            query = ToolCallQuery(call.repo, call.tool_name, call.command, 1e12)
            prediction, diagnostics = predict_call_load(runtime=runtime, trie=trie, lattice=lattice, query=query, edges=edges)
            estimates = dict(prediction.targets)
            pmu_evidence = runtime.predict_pmu_samples(query)
            for metric in ("ipc", "llc_mpki", "llc_miss_rate"):
                values = sorted(pmu_evidence.get(metric, {}).get("values", ()))
                estimates["pmu_" + metric] = SimpleNamespace(
                    p50=statistics.median(values) if values else None,
                    p90=values[math.ceil(.9 * len(values)) - 1] if values else None,
                    status="available" if values else "unavailable", backend="runtime_pmu")
            actuals = {("duration_ms" if target == "latency_ms" else target): value
                       for target, value in _target_values(call).items()
                       if target == "latency_ms" or target in estimates}
            for target, actual in actuals.items():
                estimate = estimates[target]
                row = {"task": key, "benchmark": task["benchmark"], "target": target,
                       "tool": call.tool_name, "actual": actual, "p50": estimate.p50, "p90": estimate.p90,
                       "status": estimate.status, "backend": estimate.backend,
                       "baseline_p50": statistics.median(baselines[(call.tool_name, target)]) if baselines[(call.tool_name, target)] else None}
                rows.append(row)
                metrics[(task["benchmark"], target)].append(row)
    summaries = []
    for (name, target), selected in sorted(metrics.items()):
        valid = [row for row in selected if row["p50"] is not None]
        errors = [abs(row["p50"] - row["actual"]) for row in valid]
        totals = sum(row["actual"] for row in valid)
        paired = [row for row in valid if row["baseline_p50"] is not None]
        by_task = defaultdict(list)
        for row in valid:
            by_task[row["task"]].append(abs(row["p50"] - row["actual"]))
        quantiles = [row for row in selected if row["p90"] is not None]
        summaries.append({"benchmark": name, "target": target, "eligible": len(selected), "predicted": len(valid),
            "coverage": len(valid) / len(selected), "mae": statistics.mean(errors) if errors else None,
            "wape": sum(errors) / totals if totals else None,
            "task_macro_mae": statistics.mean(statistics.mean(items) for items in by_task.values()) if by_task else None,
            "p90_coverage": statistics.mean(row["actual"] <= row["p90"] for row in quantiles) if quantiles else None,
            "baseline_paired_count": len(paired),
            "model_paired_mae": statistics.mean(abs(row["p50"] - row["actual"]) for row in paired) if paired else None,
            "baseline_mae": statistics.mean(abs(row["baseline_p50"] - row["actual"]) for row in paired) if paired else None})
    if any(digest(output / "seed" / name) != bundle["snapshots"][name] for name in FILES):
        raise RuntimeError("frozen test modified seed")
    report = {"schema": "clawtune.offline-report.v1", "train_tasks": len(manifest["train"]),
              "test_tasks": len(manifest["test"]), "test_updates": 0, "metrics": summaries,
              "scope": "tool-call prediction using execution-before command features; unavailable targets are not scored",
              "split_sha256": digest(output / "split.json")}
    validate(report, "offline-report.schema.json")
    write_json(output / "report.json", report)
    (output / "predictions.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
    (output / "report.md").write_text("# Frozen task-held-out evaluation\n\n```json\n" + json.dumps(report, indent=2) + "\n```\n", encoding="utf-8")
    return report
