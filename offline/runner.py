from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from benchmarks.bootstrap import ROOT
from benchmarks.adapters import NAMES
from clawtune_kb import FILES, create_seed
from clawtune_kb.store import digest, write_json
from cold_start.flat_loader import declared_sampling_interval_ms, read_task, LoadedTask
from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB, ToolCallQuery, _target_values
from tool_time.lattice_kb import LatticeTimeKB


def canonical_benchmark(name: str) -> str:
    if name.startswith("bfcl-"):
        return "bfcl"
    return {"deepresearchbench": "deep-research-bench", "terminalbench": "terminal-bench"}.get(name, name)


def _trace_priority(path: Path) -> int:
    if path.name == "trace.jsonl" or path.name.endswith(".trace.jsonl"):
        return 0
    if path.name == "trace.raw.jsonl":
        return 1
    return 2


def _attempt_identity(path: Path, dataset: Path, task_id: str) -> str:
    relative = path.relative_to(dataset)
    for index, part in enumerate(relative.parts[:-1]):
        if re.fullmatch(r"attempt[_-]?\d+", part, flags=re.IGNORECASE):
            return task_id + ":" + Path(*relative.parts[:index + 1]).as_posix()
    # Flat exports may contain several genuine attempts of one task. Keep each
    # file independent because no attempt directory exists to pair raw/final.
    return task_id + ":" + relative.as_posix()


def _nearest_dataset_task(path: Path, dataset: Path) -> dict:
    parent = path.parent
    while parent == dataset or parent.is_relative_to(dataset):
        side = parent / "dataset-task.json"
        if side.is_file():
            return json.loads(side.read_text(encoding="utf-8"))
        if parent == dataset:
            break
        parent = parent.parent
    return {}


def inventory(dataset: Path, benchmark: str | None, excluded: list | None = None) -> dict:
    result = {}
    excluded = excluded if excluded is not None else []
    selected_attempts = {}
    for path in sorted(dataset.rglob("*")):
        if not path.is_file() or path.suffix not in {".jsonl", ".trace"}:
            continue
        if not path.resolve().is_relative_to(dataset):
            raise ValueError("trace resolves outside the read-only dataset")
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
        metadata = {**metadata, **_nearest_dataset_task(path, dataset)}
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
        version = metadata.get("trace_format_version", metadata.get("schema_version"))
        if version not in {5, 6}:
            raise ValueError(f"{path}: only trace v5/v6 are supported")
        candidate = {"benchmark": name, "task_id": task_id, "group": group,
                     "grouping": "repository" if repo else "category" if category else "dataset",
                     "path": path.relative_to(dataset).as_posix(), "sha256": digest(path),
                     "version": version, "priority": _trace_priority(path)}
        attempt = (key, _attempt_identity(path, dataset, task_id))
        previous = selected_attempts.get(attempt)
        if previous is not None:
            winner, loser = ((candidate, previous) if (candidate["priority"], candidate["path"])
                             < (previous["priority"], previous["path"])
                             else (previous, candidate))
            selected_attempts[attempt] = winner
            excluded.append({"path": loser["path"], "reason": f"preferred canonical trace {winner['path']} in this attempt"})
        else:
            selected_attempts[attempt] = candidate
    for candidate in sorted(selected_attempts.values(), key=lambda row: row["path"]):
        key = f"{candidate['benchmark']}:{candidate['task_id']}"
        entry = {field: candidate[field] for field in ("benchmark", "task_id", "group", "grouping")}
        entry["files"] = []
        if key in result and any(result[key][field] != entry[field] for field in ("group", "grouping")):
            raise ValueError(f"inconsistent task identity: {key}")
        stored = result.setdefault(key, entry)
        stored["files"].append({field: candidate[field] for field in ("path", "sha256", "version")})
    if not result:
        raise ValueError("no fixed-format task traces found")
    return result


def split_tasks(tasks: dict, seed: int, train_fraction: float = .8) -> dict:
    if not math.isfinite(train_fraction) or not 0 < train_fraction < 1:
        raise ValueError("train fraction must be greater than 0 and less than 1")
    groups = defaultdict(list)
    for key, task in tasks.items():
        groups[(task["benchmark"], task["group"])].append(key)
    train, test, counts = [], [], []
    for (benchmark, group), members in sorted(groups.items()):
        members.sort(key=lambda key: hashlib.sha256(
            f"clawtune-fixed-name-split-v2\0{seed}\0{benchmark}\0{group}\0{key}".encode()
        ).hexdigest())
        n = max(1, math.floor(train_fraction * len(members)))
        train.extend(members[:n])
        test.extend(members[n:])
        counts.append({"benchmark": benchmark, "group": group, "train": n, "test": len(members) - n})
    return {"schema": "clawtune.split.v2", "seed": seed, "train_fraction": train_fraction,
            "rule": "sha256-fixed-name-per-benchmark-group-task-v2", "tasks": tasks,
            "train": sorted(train), "test": sorted(test), "groups": counts}


def _task_roster_sha256(tasks: dict) -> str:
    roster = [
        {field: task[field] for field in ("benchmark", "task_id", "group", "grouping")}
        for task in sorted(tasks.values(), key=lambda row: (row["benchmark"], row["task_id"]))
    ]
    return hashlib.sha256(json.dumps(roster, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _split_assignment_sha256(manifest: dict) -> str:
    """Hash only the logical assignment, never paths or registry state."""
    assignment = {
        field: manifest[field]
        for field in ("schema", "seed", "train_fraction", "rule", "train", "test", "groups")
    }
    return hashlib.sha256(
        json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_or_create_split(tasks: dict, dataset: Path, seed: int, cache_dir: Path,
                         train_fraction: float = .8) -> dict:
    """Persist the first deterministic assignment and reuse it by task roster."""
    from clawtune_kb.contracts import validate
    roster_sha256 = _task_roster_sha256(tasks)
    benchmark_names = sorted({task["benchmark"] for task in tasks.values()})
    dataset_id = "+".join(benchmark_names) + ":" + roster_sha256
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", "+".join(benchmark_names)) or "dataset"
    cache_dir = cache_dir.resolve()
    fraction_suffix = "" if train_fraction == .8 else f"-train{format(train_fraction, '.12g').replace('.', 'p')}"
    cache_path = cache_dir / f"{safe_name}-{roster_sha256[:16]}{fraction_suffix}-seed{seed}.json"
    base = split_tasks(tasks, seed, train_fraction)
    base["assignment_sha256"] = _split_assignment_sha256(base)
    metadata = {
        "dataset_id": dataset_id,
        "dataset_name": dataset.name,
        "task_roster_sha256": roster_sha256,
        "registered_source_path": str(dataset),
        "current_source_path": str(dataset),
        "split_registry_path": str(cache_path),
    }
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        validate(cached, "offline-split.schema.json")
        cached_train = set(cached.get("train", ()))
        cached_test = set(cached.get("test", ()))
        if (cached.get("dataset_id") != dataset_id or cached.get("seed") != seed
                or cached.get("train_fraction") != train_fraction
                or cached_train | cached_test != set(tasks)
                or cached_train & cached_test
                or cached.get("assignment_sha256") != base["assignment_sha256"]):
            raise ValueError(f"cached split does not match dataset task roster: {cache_path}")
        return {**base, **metadata,
                "registered_source_path": cached.get("registered_source_path", str(dataset)),
                "train": cached["train"], "test": cached["test"], "groups": cached["groups"],
                "split_registry_status": "reused"}
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = {**base, **metadata, "split_registry_status": "created"}
    validate(cached, "offline-split.schema.json")
    write_json(cache_path, cached)
    return cached


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
            calls_v6 = [replace(call, repo=namespace) for call in loaded_v6.completed_calls]
            loaded = LoadedTask(calls=calls_v6, call_actuals=[{
                ("duration_ms" if target == "latency_ms" else target): value
                for target, value in _target_values(call).items()
            } for call in calls_v6], call_clauses=[() for _ in calls_v6])
            from tool_resource.sdk import _observations_from_call
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    if row.get("record_type") != "span_end" or row.get("kind") != "tool":
                        continue
                    sample_interval_ms = declared_sampling_interval_ms(row.get("resources") or {})
                    if sample_interval_ms is not None:
                        loaded.sample_periods_ms.append(sample_interval_ms)
                    telemetry = (row.get("execution") or {}).get("tool_resource") or {}
                    call = telemetry.get("call_telemetry") or {}
                    if call.get("eligible_for_kb") is True and call.get("telemetry_quality") == "ok":
                        loaded.clauses.extend(_observations_from_call(namespace, call, require_timestamps=False))
        # Static train/test has no chronological replay semantics.
        result.calls.extend(replace(call, ts_start=0., ts_end=call.ts_end - call.ts_start) for call in loaded.calls)
        result.call_actuals.extend(loaded.call_actuals)
        result.call_clauses.extend(loaded.call_clauses)
        result.clauses.extend(replace(row, ts_start=0., ts_end=row.ts_end - row.ts_start) for row in loaded.clauses)
        result.sample_periods_ms.extend(loaded.sample_periods_ms)
        result.counts.update(loaded.counts)
    return result


def _sampling_summary(values: list[float]) -> dict:
    selected = sorted(value for value in values if math.isfinite(value) and value > 0)
    if not selected:
        return {"available": False, "observed_calls": 0, "min_ms": None,
                "p50_ms": None, "max_ms": None}
    return {"available": True, "observed_calls": len(selected), "min_ms": selected[0],
            "p50_ms": statistics.median(selected), "max_ms": selected[-1]}


def _safe_recorded_clause(command: str | None, clauses: tuple[dict, ...]) -> tuple[dict, ...] | None:
    """Use trace-provided argv only for an unambiguous plain single command."""
    if not command or len(clauses) != 1 or re.search(r"[|&<>$`(){}=!#*?\[\]~;\n]", command):
        return None
    clause = clauses[0]
    argv = clause.get("argv")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not isinstance(argv, (list, tuple)) or tokens != list(argv):
        return None
    from tool_resource.features import shell_bin_requires_exec_evidence
    return clauses if shell_bin_requires_exec_evidence(str(clause.get("bin") or ""), argv[0]) else None


def _nearest_rank(values: list[float], quantile: float) -> float | None:
    selected = sorted(values)
    return selected[math.ceil(quantile * len(selected)) - 1] if selected else None


def _bucket_summary(rows: list[dict], edges: tuple[float, ...]) -> dict:
    bucket_count = len(edges) + 1
    eligible = [row for row in rows if row.get("actual_bucket") is not None]
    predicted = [row for row in eligible if row.get("predicted_bucket") is not None]
    matrix = [[0] * bucket_count for _ in range(bucket_count)]
    brier_scores = []
    for row in predicted:
        actual, guess = row["actual_bucket"], row["predicted_bucket"]
        matrix[actual][guess] += 1
        probabilities = row.get("bucket_probabilities")
        if isinstance(probabilities, list) and len(probabilities) == bucket_count:
            brier_scores.append(sum(
                (probability - (1.0 if index == actual else 0.0)) ** 2
                for index, probability in enumerate(probabilities)
            ))
    per_bucket = []
    for index in range(bucket_count):
        tp = matrix[index][index]
        support = sum(matrix[index])
        predicted_count = sum(matrix[row][index] for row in range(bucket_count))
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / support if support else None
        f1 = (2 * precision * recall / (precision + recall)
              if recall is not None and precision + recall else 0.0 if recall is not None else None)
        per_bucket.append({
            "bucket": index,
            "lower_inclusive_ms": None if index == 0 else edges[index - 1],
            "upper_exclusive_ms": edges[index] if index < len(edges) else None,
            "support": support,
            "predicted_count": predicted_count,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        })
    supported = [row for row in per_bucket if row["support"]]
    return {
        "edges_ms": list(edges),
        "interval": "left_closed_right_open",
        "eligible": len(eligible),
        "predicted": len(predicted),
        "coverage": len(predicted) / len(eligible) if eligible else 0.0,
        "accuracy": (sum(matrix[index][index] for index in range(bucket_count)) / len(predicted)
                     if predicted else None),
        "macro_recall": (statistics.mean(row["recall"] for row in supported) if supported else None),
        "brier_score": statistics.mean(brier_scores) if brier_scores else None,
        "per_bucket": per_bucket,
        "confusion_matrix": matrix,
    }


def _metric_summary(name: str, target: str, rows: list[dict],
                    duration_edges: tuple[float, ...]) -> dict:
    valid = [row for row in rows if row["p50"] is not None]
    errors = [abs(row["p50"] - row["actual"]) for row in valid]
    signed_errors = [row["p50"] - row["actual"] for row in valid]
    total_actual = sum(row["actual"] for row in valid)
    paired = [row for row in valid if row["baseline_p50"] is not None]
    by_task = defaultdict(list)
    for row, error in zip(valid, errors, strict=True):
        by_task[row["task"]].append(error)
    quantiles = [row for row in valid if row["p90"] is not None]
    positive = [row for row in valid if row["actual"] > 0]
    result = {
        "benchmark": name,
        "target": target,
        "unit": rows[0]["unit"] if rows else None,
        "eligible": len(rows),
        "predicted": len(valid),
        "coverage": len(valid) / len(rows) if rows else 0.0,
        "unavailable_reasons": dict(Counter(
            str(row.get("unavailable_reason") or "unknown")
            for row in rows if row["p50"] is None
        )),
        "evaluated_tasks": len(by_task),
        "mae": statistics.mean(errors) if errors else None,
        "median_absolute_error": statistics.median(errors) if errors else None,
        "p90_absolute_error": _nearest_rank(errors, .9),
        "rmse": math.sqrt(statistics.mean(error * error for error in signed_errors)) if signed_errors else None,
        "mean_error_bias": statistics.mean(signed_errors) if signed_errors else None,
        "wape": sum(errors) / total_actual if total_actual else None,
        "smape": (statistics.mean(2 * abs(row["p50"] - row["actual"])
                                   / (abs(row["p50"]) + abs(row["actual"]))
                                   if row["p50"] or row["actual"] else 0.0 for row in valid)
                  if valid else None),
        "within_2x": (statistics.mean(
            row["p50"] > 0 and max(row["p50"] / row["actual"], row["actual"] / row["p50"]) <= 2
            for row in positive) if positive else None),
        "task_macro_mae": (statistics.mean(statistics.mean(items) for items in by_task.values())
                           if by_task else None),
        "p90_coverage": (statistics.mean(row["actual"] <= row["p90"] for row in quantiles)
                         if quantiles else None),
        "p90_pinball_loss": (statistics.mean(
            .9 * (row["actual"] - row["p90"])
            if row["actual"] >= row["p90"] else .1 * (row["p90"] - row["actual"])
            for row in quantiles) if quantiles else None),
        "baseline_paired_count": len(paired),
        "model_paired_mae": (statistics.mean(abs(row["p50"] - row["actual"]) for row in paired)
                             if paired else None),
        "baseline_mae": (statistics.mean(abs(row["baseline_p50"] - row["actual"]) for row in paired)
                         if paired else None),
    }
    if target == "duration_ms":
        result["bucket_metrics"] = _bucket_summary(rows, duration_edges)
    return result


def _summarize_rows(rows: list[dict], duration_edges: tuple[float, ...]) -> list[dict]:
    selected = defaultdict(list)
    for row in rows:
        selected[(row["benchmark"], row["target"])].append(row)
    return [_metric_summary(name, target, values, duration_edges)
            for (name, target), values in sorted(selected.items())]


def _display(value, *, percent: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{100 * value:.1f}%" if percent else f"{value:.6g}"


def _render_report(report: dict) -> str:
    lines = [
        "# Frozen task-held-out evaluation", "",
        f"Train tasks: {report['train_tasks']}; test tasks: {report['test_tasks']}; test updates: 0.", "",
        "## Global metrics", "",
        "| Target | Unit | Predicted / eligible | MAE | Median AE | P90 AE | RMSE | Bias | WAPE | sMAPE | Within 2x | P90 coverage | P90 pinball | Bucket accuracy | Bucket macro recall |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for metric in report["metrics"]:
        bucket = metric.get("bucket_metrics", {})
        lines.append("| " + " | ".join([
            metric["target"], str(metric["unit"]), f"{metric['predicted']} / {metric['eligible']}",
            _display(metric["mae"]), _display(metric["median_absolute_error"]),
            _display(metric["p90_absolute_error"]), _display(metric["rmse"]),
            _display(metric["mean_error_bias"]), _display(metric["wape"], percent=True),
            _display(metric["smape"], percent=True), _display(metric["within_2x"], percent=True),
            _display(metric["p90_coverage"], percent=True), _display(metric["p90_pinball_loss"]),
            _display(bucket.get("accuracy"), percent=True),
            _display(bucket.get("macro_recall"), percent=True),
        ]) + " |")
    duration = next((metric.get("bucket_metrics") for metric in report["metrics"]
                     if metric["target"] == "duration_ms"), None)
    if duration:
        lines.extend(["", "## Duration buckets", "",
                      "| Bucket | Interval ms | Support | Predicted | Precision | Recall | F1 |",
                      "|---:|---|---:|---:|---:|---:|---:|"])
        for bucket in duration["per_bucket"]:
            lower = bucket["lower_inclusive_ms"]
            upper = bucket["upper_exclusive_ms"]
            interval = f"[{lower if lower is not None else 0}, {upper if upper is not None else 'inf'})"
            lines.append(f"| {bucket['bucket']} | {interval} | {bucket['support']} | "
                         f"{bucket['predicted_count']} | {_display(bucket['precision'], percent=True)} | "
                         f"{_display(bucket['recall'], percent=True)} | {_display(bucket['f1'], percent=True)} |")
    lines.extend(["", "## Repository metrics", "",
                  "| Benchmark | Repository/category | Train tasks | Test tasks | Evaluated test tasks | Target | Predicted / eligible | MAE | RMSE | WAPE | Bucket accuracy | Bucket macro recall |",
                  "|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|"])
    for repo in report["repositories"]:
        repo_metrics = repo["metrics"] or [None]
        for metric in repo_metrics:
            bucket = metric.get("bucket_metrics", {}) if metric else {}
            lines.append("| " + " | ".join([
                repo["benchmark"], repo["repo"], str(repo["train_tasks"]), str(repo["test_tasks"]),
                str(repo["evaluated_test_tasks"]), metric["target"] if metric else "N/A",
                f"{metric['predicted']} / {metric['eligible']}" if metric else "N/A",
                _display(metric["mae"]) if metric else "N/A",
                _display(metric["rmse"]) if metric else "N/A",
                _display(metric["wape"], percent=True) if metric else "N/A",
                _display(bucket.get("accuracy"), percent=True),
                _display(bucket.get("macro_recall"), percent=True),
            ]) + " |")
    return "\n".join(lines) + "\n"


def run(dataset: Path, output: Path, *, benchmark: str | None = None, seed: int = 42,
        rss_unit: str = "MiB", split_cache_dir: Path | None = None,
        train_fraction: float = .8) -> dict:
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
        reports = {name: run(dataset, output / name, benchmark=name, seed=seed, rss_unit=rss_unit,
                             split_cache_dir=split_cache_dir, train_fraction=train_fraction)
                   for name in dataset_names}
        report = {"schema": "clawtune.offline-report.v1", "train_tasks": sum(r["train_tasks"] for r in reports.values()),
                  "test_tasks": sum(r["test_tasks"] for r in reports.values()), "test_updates": 0,
                  "datasets": reports, "metrics": [m for r in reports.values() for m in r["metrics"]],
                  "repositories": [repo for value in reports.values() for repo in value["repositories"]],
                  "pmu": {"scope": "per-dataset quality-gated ToolKB prediction availability",
                          "datasets": {name: value["pmu"] for name, value in reports.items()}}}
        validate(report, "offline-report.schema.json")
        write_json(output / "report.json", report)
        (output / "report.md").write_text("# Independent dataset evaluations\n\n" + "\n".join(f"- [{name}]({name}/report.md)" for name in dataset_names) + "\n", encoding="utf-8")
        return report
    cache_dir = split_cache_dir or ROOT / ".runtime" / "offline" / "splits"
    manifest = load_or_create_split(tasks, dataset, seed, cache_dir, train_fraction)
    manifest["excluded_files"] = excluded
    validate(manifest, "offline-split.schema.json")
    if not manifest["test"]:
        raise ValueError("no test tasks: every group is a singleton")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "split.json", manifest)
    clauses, calls, call_actuals, counts = [], [], [], Counter()
    train_sampling = {}
    for key in manifest["train"]:
        loaded = load_task(dataset, tasks[key], rss_unit)
        clauses.extend(loaded.clauses)
        calls.extend(loaded.calls)
        call_actuals.extend(loaded.call_actuals)
        train_sampling[key] = _sampling_summary(loaded.sample_periods_ms)
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
        provenance={"split_sha256": manifest["assignment_sha256"], "train": manifest["train"],
                    "test": manifest["test"], "rss_unit": rss_unit, "counts": dict(counts),
                    "resource_sampling": {"unit": "ms", "per_task": train_sampling}})
    trie.freeze()
    runtime.freeze()
    lattice.freeze()
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.prediction_config import load_bucket_edges
    edges = load_bucket_edges((100, 500, 2000, 10000))
    baselines = defaultdict(list)
    for index, call in enumerate(calls):
        if not call.censored:
            actuals = {("duration_ms" if target == "latency_ms" else target): value
                       for target, value in _target_values(call).items()}
            if index < len(call_actuals):
                actuals.update(call_actuals[index])
            for target, value in actuals.items():
                baselines[(call.tool_name, target)].append(value)
    rows = []
    pmu_availability = {
        metric: {"queries": 0, "available_predictions": 0, "labeled": 0}
        for metric in ("ipc", "llc_mpki", "llc_miss_rate")
    }
    test_sampling = {}
    for key in manifest["test"]:
        task = tasks[key]
        loaded = load_task(dataset, task, rss_unit)
        test_sampling[key] = _sampling_summary(loaded.sample_periods_ms)
        for index, call in enumerate(loaded.calls):
            if call.censored:
                continue
            query = ToolCallQuery(call.repo, call.tool_name, call.command, 1e12)
            recorded_clauses = loaded.call_clauses[index] if index < len(loaded.call_clauses) else ()
            known_clauses = _safe_recorded_clause(call.command, recorded_clauses)
            prediction, diagnostics = predict_call_load(
                runtime=runtime, trie=trie, lattice=lattice, query=query, edges=edges,
                parsed_clauses=known_clauses,
            )
            estimates = dict(prediction.targets)
            pmu_evidence = runtime.predict_pmu_samples(query)
            for metric in ("ipc", "llc_mpki", "llc_miss_rate"):
                values = sorted(pmu_evidence.get(metric, {}).get("values", ()))
                estimates["pmu_" + metric] = SimpleNamespace(
                    p50=statistics.median(values) if values else None,
                    p90=values[math.ceil(.9 * len(values)) - 1] if values else None,
                    status="available" if values else "unavailable", backend="runtime_pmu",
                    unit={"ipc": "ratio", "llc_mpki": "misses_per_1000_instructions",
                          "llc_miss_rate": "ratio"}[metric], buckets=None,
                    unavailable_reason=None if values else "no_quality_gated_pmu_history")
                pmu_availability[metric]["queries"] += 1
                pmu_availability[metric]["available_predictions"] += int(bool(values))
            actual_target_values = {("duration_ms" if target == "latency_ms" else target): value
                                    for target, value in _target_values(call).items()}
            if index < len(loaded.call_actuals):
                actual_target_values.update(loaded.call_actuals[index])
            for metric in pmu_availability:
                pmu_availability[metric]["labeled"] += int("pmu_" + metric in actual_target_values)
            actuals = {target: value for target, value in actual_target_values.items()
                       if target in estimates}
            for target, actual in actuals.items():
                estimate = estimates[target]
                bucket = getattr(estimate, "buckets", None)
                probabilities = list(bucket.probabilities) if (target == "duration_ms" and bucket
                                                               and bucket.probabilities is not None) else None
                row = {"task": key, "benchmark": task["benchmark"], "repo": task["group"],
                       "target": target, "unit": getattr(estimate, "unit", None),
                       "tool": call.tool_name, "actual": actual, "p50": estimate.p50, "p90": estimate.p90,
                       "status": estimate.status, "backend": estimate.backend,
                       "unavailable_reason": getattr(estimate, "unavailable_reason", None),
                       "baseline_p50": statistics.median(baselines[(call.tool_name, target)]) if baselines[(call.tool_name, target)] else None}
                if target == "duration_ms":
                    row.update({
                        "actual_bucket": bisect_right(edges["duration_ms"], actual),
                        "predicted_bucket": (max(range(len(probabilities)),
                                                 key=lambda value: (probabilities[value], -value))
                                             if probabilities else None),
                        "bucket_probabilities": probabilities,
                    })
                rows.append(row)
    summaries = _summarize_rows(rows, edges["duration_ms"])
    rows_by_repo = defaultdict(list)
    for row in rows:
        rows_by_repo[(row["benchmark"], row["repo"])].append(row)
    repositories = []
    for group in manifest["groups"]:
        selected = rows_by_repo[(group["benchmark"], group["group"])]
        grouping = next(task["grouping"] for task in tasks.values()
                        if task["benchmark"] == group["benchmark"] and task["group"] == group["group"])
        repositories.append({
            "benchmark": group["benchmark"], "repo": group["group"], "grouping": grouping,
            "train_tasks": group["train"], "test_tasks": group["test"],
            "evaluated_test_tasks": len({row["task"] for row in selected}),
            "metrics": _summarize_rows(selected, edges["duration_ms"]),
        })
    if any(digest(output / "seed" / name) != bundle["snapshots"][name] for name in FILES):
        raise RuntimeError("frozen test modified seed")
    report = {"schema": "clawtune.offline-report.v1", "train_tasks": len(manifest["train"]),
              "test_tasks": len(manifest["test"]), "test_updates": 0, "metrics": summaries,
              "bucket_edges": {target: list(values) for target, values in edges.items()},
              "repositories": repositories,
              "pmu": {"scope": "quality-gated ToolKB prediction availability",
                      "targets": pmu_availability},
              "resource_sampling": {
                  "definition": "declared per-call interval (v5 sample_interval_s or v6 sampling_interval_ms); labels are not rescaled",
                  "cpu_peak_rule": "only independently verified fixed 500ms clause windows are eligible",
                  "memory_rule": "sampled peak RSS remains sampling-frequency dependent",
                  "train": train_sampling, "test": test_sampling,
              },
              "split_registry_path": manifest["split_registry_path"],
              "split_registry_status": manifest["split_registry_status"],
              "scope": "tool-call prediction using execution-before command features; unavailable targets are not scored",
              "split_sha256": manifest["assignment_sha256"]}
    validate(report, "offline-report.schema.json")
    write_json(output / "report.json", report)
    (output / "predictions.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
    (output / "report.md").write_text(_render_report(report), encoding="utf-8")
    return report
