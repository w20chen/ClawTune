"""Validate thresholded heavy-resource rules against a fixed trace corpus.

This is a research validator, not a new public protocol.  It deliberately
uses the existing ClauseResourceKB backoff policy and keeps the legacy
``sampled_peak_rss_mb`` measurement separate from ClawTune's production
environment-memory targets.

Example::

    python tools/validate_heavy_resource_rules.py \
      --dataset C:/data/swe-rebench-original-flat-644-20260904 \
      --output .runtime/heavy-resource-validation
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "sidecar" / "src"))

from tool_resource.runtime_kb import (  # noqa: E402
    ClauseObservation,
    ClauseResourceKB,
    _SAMPLED_PEAK_RSS_MB,
    _clause_public_keys,
    _clause_repo_keys,
    _clause_value,
    is_pipeline_dependent_consumer,
)
from tool_resource.commands import normalized_observation  # noqa: E402


CPU_SOURCE = "load:cpu_time_seconds"
RSS_SOURCE = _SAMPLED_PEAK_RSS_MB
DEFAULT_CPU_THRESHOLD_S = 1.0
DEFAULT_RSS_THRESHOLD_MB = 128.0
CPU_CANDIDATES_S = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
RSS_CANDIDATES_MB = (128.0, 256.0, 512.0, 1024.0, 2048.0, 4096.0)


@dataclass(frozen=True)
class TraceObservations:
    task_key: str
    benchmark: str
    group: str
    model: str
    path: str
    observations: tuple[ClauseObservation, ...]
    resource_actions: int
    eligible_actions: int
    invalid_clauses: int


@dataclass(frozen=True)
class Evidence:
    values: tuple[float, ...]
    scope: str
    key_kind: str

    @property
    def p50(self) -> float:
        return float(statistics.median(self.values))

    @property
    def p90(self) -> float:
        return sorted(self.values)[max(0, math.ceil(0.9 * len(self.values)) - 1)]


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _resolve_dataset(path: Path) -> Path:
    path = path.resolve()
    if (path / "MANIFEST.jsonl").is_file():
        return path
    children = [child for child in path.iterdir() if child.is_dir()]
    matches = [child for child in children if (child / "MANIFEST.jsonl").is_file()]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"dataset must contain MANIFEST.jsonl or exactly one manifest child: {path}"
    )


def _group_from_instance(instance_id: str) -> str:
    # SWE-ReBench instance ids use the final numeric component as the issue id.
    return re.sub(r"-\d+$", "", instance_id) or instance_id


def _manifest(dataset: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with (dataset / "MANIFEST.jsonl").open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            name = row.get("flattened_name")
            instance_id = row.get("instance_id")
            corpus = row.get("corpus")
            if not isinstance(name, str) or not isinstance(instance_id, str):
                raise ValueError(f"manifest line {line_no}: missing flattened_name/instance_id")
            if not isinstance(corpus, str) or not corpus:
                raise ValueError(f"manifest line {line_no}: missing corpus")
            if name in result:
                raise ValueError(f"manifest has duplicate flattened_name: {name}")
            result[name] = {
                "benchmark": corpus,
                "instance_id": instance_id,
                "group": _group_from_instance(instance_id),
                "model": str(row.get("model") or "unknown"),
                "status": str(row.get("status") or "unknown"),
            }
    return result


def _observation_from_clause(
    repo: str, row: Mapping[str, Any]
) -> ClauseObservation | None:
    availability = row.get("availability")
    if not isinstance(availability, Mapping) or availability.get("latency") != "ok":
        return None
    bin_ = row.get("bin")
    argv = row.get("argv")
    ts_start = _finite_nonnegative(row.get("ts_start"))
    ts_end = _finite_nonnegative(row.get("ts_end"))
    latency_ms = _finite_nonnegative(row.get("latency_ms"))
    if (
        not isinstance(bin_, str)
        or not bin_
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(value, str) for value in argv)
        or ts_start is None
        or ts_end is None
        or ts_end < ts_start
        or latency_ms is None
    ):
        return None
    cpu_ns = row.get("cpu_ns_cumulative")
    if isinstance(cpu_ns, bool) or not isinstance(cpu_ns, int) or cpu_ns < 0:
        cpu_ns = None
    rss_mb = _finite_nonnegative(row.get("sampled_peak_rss_mb"))
    peak_cpu = _finite_nonnegative(row.get("peak_cpu_cores"))
    return ClauseObservation(
        repo=repo,
        bin=str(bin_),
        argv=tuple(argv),
        ts_start=ts_start,
        ts_end=ts_end,
        latency_ms=latency_ms,
        cpu_peak_cores=peak_cpu,
        sampled_peak_rss_mb=rss_mb,
        cpu_ns_cumulative=cpu_ns,
        in_loop=bool(row.get("in_loop", False)),
        in_pipe=bool(row.get("in_pipe", False)),
        in_subst=bool(row.get("in_subst", False)),
        pipeline_position=int(row.get("pipeline_position", -1)),
    )


def _read_trace(path: Path, manifest_row: Mapping[str, Any]) -> TraceObservations:
    benchmark = str(manifest_row["benchmark"])
    group = str(manifest_row["group"])
    task_key = f"{benchmark}:{manifest_row['instance_id']}"
    # Match the offline runner's repository namespace without importing its
    # CLI and without treating the read-only corpus as an offline seed.
    repo = f"{benchmark}:{group}"
    observations: list[ClauseObservation] = []
    resource_actions = 0
    eligible_actions = 0
    invalid_clauses = 0
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("type") != "action" or record.get("action_type") != "tool_exec":
                continue
            data = record.get("data") or {}
            resource = data.get("resource_observation")
            if not isinstance(resource, Mapping):
                continue
            resource_actions += 1
            if resource.get("eligible_for_kb") is not True:
                continue
            eligible_actions += 1
            clauses = resource.get("clauses")
            if not isinstance(clauses, list):
                invalid_clauses += 1
                continue
            for row in clauses:
                if not isinstance(row, Mapping):
                    invalid_clauses += 1
                    continue
                observation = _observation_from_clause(repo, row)
                if observation is None:
                    invalid_clauses += 1
                else:
                    observations.append(observation)
    return TraceObservations(
        task_key=task_key,
        benchmark=benchmark,
        group=group,
        model=str(manifest_row["model"]),
        path=str(path),
        observations=tuple(observations),
        resource_actions=resource_actions,
        eligible_actions=eligible_actions,
        invalid_clauses=invalid_clauses,
    )


def load_dataset(dataset_arg: Path) -> tuple[Path, list[TraceObservations], dict[str, Any]]:
    dataset = _resolve_dataset(dataset_arg)
    manifest = _manifest(dataset)
    traces: list[TraceObservations] = []
    trace_files_seen = 0
    for name, row in sorted(manifest.items()):
        path = dataset / name
        if not path.is_file():
            raise ValueError(f"manifest file is missing: {path}")
        trace_files_seen += 1
        loaded = _read_trace(path, row)
        if loaded.observations:
            traces.append(loaded)
    stats = {
        "manifest_files": len(manifest),
        "trace_files_seen": trace_files_seen,
        "resource_bearing_traces": len(traces),
        "resource_actions": sum(row.resource_actions for row in traces),
        "eligible_resource_actions": sum(row.eligible_actions for row in traces),
        "valid_clauses": sum(len(row.observations) for row in traces),
        "invalid_clauses": sum(row.invalid_clauses for row in traces),
        "models_with_resource_traces": dict(Counter(row.model for row in traces)),
        "benchmarks_with_resource_traces": dict(Counter(row.benchmark for row in traces)),
    }
    return dataset, traces, stats


def _task_split(
    traces: Sequence[TraceObservations], seed: int, train_fraction: float
) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    if not 0.0 < train_fraction < 1.0 or not math.isfinite(train_fraction):
        raise ValueError("train_fraction must be strictly between 0 and 1")
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    trace_by_task = {trace.task_key: trace for trace in traces}
    for task_key, trace in trace_by_task.items():
        grouped[(trace.benchmark, trace.group)].append(task_key)
    train: set[str] = set()
    test: set[str] = set()
    group_rows: list[dict[str, Any]] = []
    for (benchmark, group), members in sorted(grouped.items()):
        members.sort(
            key=lambda key: hashlib.sha256(
                f"clawtune-fixed-name-split-v2\0{seed}\0{benchmark}\0{group}\0{key}".encode()
            ).hexdigest()
        )
        n_train = max(1, math.floor(train_fraction * len(members)))
        train.update(members[:n_train])
        test.update(members[n_train:])
        group_rows.append(
            {
                "benchmark": benchmark,
                "group": group,
                "train": n_train,
                "test": len(members) - n_train,
            }
        )
    if train & test or train | test != set(trace_by_task):
        raise AssertionError("task split is not a partition")
    return train, test, group_rows


def _standalone_value(obs: ClauseObservation, source: str) -> float | None:
    if is_pipeline_dependent_consumer(obs) or obs.in_loop or obs.in_subst:
        return None
    return _clause_value(obs, source)


class _ResearchRssKB:
    """Research-only RSS index with the ClauseResourceKB key/backoff policy.

    The production ClauseResourceKB intentionally does not store RSS. Keeping
    this small index local to the validator makes that boundary explicit while
    still testing whether the same exact/prefix/bin idea is useful for a
    future, separately specified memory signal.
    """

    def __init__(self, observations: Sequence[ClauseObservation], *, include_repo: bool) -> None:
        self._public: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._repo: dict[str, dict[tuple[str, str], list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for raw in observations:
            obs = normalized_observation(raw)
            value = _standalone_value(obs, RSS_SOURCE)
            if value is None:
                continue
            for key in _clause_public_keys(obs.bin):
                self._public[key].append(value)
            if include_repo:
                for key in _clause_repo_keys(obs.bin, obs.argv):
                    self._repo[obs.repo][key].append(value)

    def select(self, obs: ClauseObservation) -> Evidence | None:
        obs = normalized_observation(obs)
        for key in _clause_repo_keys(obs.bin, obs.argv):
            values = self._repo.get(obs.repo, {}).get(key, ())
            if values:
                return Evidence(tuple(values), "repo", key[0])
        for key in _clause_public_keys(obs.bin):
            if key[0] == "global":
                continue
            values = self._public.get(key, ())
            if values:
                return Evidence(tuple(values), "public", key[0])
        return None


def _build_kb(
    observations: Sequence[ClauseObservation], source: str, *, include_repo: bool
) -> Any:
    training = [obs for obs in observations if _standalone_value(obs, source) is not None]
    if not training:
        raise ValueError(f"training set has no eligible values for {source}")
    if source == RSS_SOURCE:
        return _ResearchRssKB(training, include_repo=include_repo)
    kb = ClauseResourceKB.fit_public(training)
    if include_repo:
        kb.merge_historical(training)
        # Absorb all training observations before freezing. This is equivalent
        # to the offline frozen seed: test outcomes never enter the KB.
        kb.predict_load_samples(training[0].repo, (), 1.0e30)
    kb.freeze()
    return kb


def _evidence(
    kb: Any, obs: ClauseObservation, source: str
) -> Evidence | None:
    if source == RSS_SOURCE:
        return kb.select(obs)
    obs = normalized_observation(obs)
    selected = kb._select(obs.repo, source, obs.bin, obs.argv)  # type: ignore[attr-defined]
    if selected is None:
        return None
    values, scope, key_kind, _path = selected
    if key_kind == "global":
        # Match ClauseResourceKB.predict_load_samples: unrelated global
        # samples are not a workload prediction.
        return None
    valid = tuple(float(value) for value in values if _finite_nonnegative(value) is not None)
    return Evidence(valid, scope, key_kind) if valid else None


def _metrics(rows: Sequence[dict[str, Any]], *, threshold: float, point: str) -> dict[str, Any]:
    covered = [row for row in rows if row.get(point) is not None]
    labels = [int(row["actual"] >= threshold) for row in rows]
    predictions = [int(row[point] >= threshold) for row in covered]
    actuals = [int(row["actual"] >= threshold) for row in covered]
    tp = sum(a == 1 and p == 1 for a, p in zip(actuals, predictions))
    tn = sum(a == 0 and p == 0 for a, p in zip(actuals, predictions))
    fp = sum(a == 0 and p == 1 for a, p in zip(actuals, predictions))
    fn = sum(a == 1 and p == 0 for a, p in zip(actuals, predictions))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    balanced = (recall + specificity) / 2.0
    prevalence = sum(actuals) / len(actuals) if actuals else 0.0
    all_light_accuracy = (tn + fp) / len(covered) if covered else None
    full_predictions = [
        int(row[point] is not None and row[point] >= threshold) for row in rows
    ]
    full_accuracy = (
        sum(actual == prediction for actual, prediction in zip(labels, full_predictions)) / len(rows)
        if rows
        else None
    )
    return {
        "labeled": len(rows),
        "covered": len(covered),
        "coverage": len(covered) / len(rows) if rows else 0.0,
        "accuracy_on_covered": (tp + tn) / len(covered) if covered else None,
        "heavy_prevalence_on_covered": prevalence if covered else None,
        "baseline_all_light_accuracy_on_covered": all_light_accuracy,
        "accuracy_gain_vs_all_light": (
            (tp + tn) / len(covered) - all_light_accuracy if covered else None
        ),
        "balanced_accuracy_gain_vs_chance": balanced - 0.5 if covered else None,
        "accuracy_if_unavailable_is_light": full_accuracy,
        "balanced_accuracy_on_covered": balanced if covered else None,
        "precision_heavy": precision if covered else None,
        "recall_heavy": recall if covered else None,
        "specificity_light": specificity if covered else None,
        "f1_heavy": f1 if covered else None,
        "confusion_matrix_on_covered": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        "actual_heavy": sum(labels),
        "predicted_heavy_on_covered": sum(predictions),
    }


def _distribution(
    rows: Sequence[ClauseObservation], source: str, thresholds: Sequence[float]
) -> dict[str, Any]:
    values = [value for obs in rows if (value := _standalone_value(obs, source)) is not None]
    ordered = sorted(values)
    q = lambda fraction: ordered[max(0, math.ceil(fraction * len(ordered)) - 1)] if ordered else None
    return {
        "labeled": len(values),
        "q50": q(0.50),
        "q75": q(0.75),
        "q90": q(0.90),
        "q95": q(0.95),
        "q99": q(0.99),
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
        "thresholds": {
            str(threshold): {
                "heavy": sum(value >= threshold for value in values),
                "heavy_fraction": sum(value >= threshold for value in values) / len(values)
                if values
                else 0.0,
            }
            for threshold in thresholds
        },
    }


def _evaluate_method(
    train: Sequence[ClauseObservation],
    test: Sequence[ClauseObservation],
    source: str,
    thresholds: Sequence[float],
    *,
    include_repo: bool,
) -> dict[str, Any]:
    kb = _build_kb(train, source, include_repo=include_repo)
    rows: list[dict[str, Any]] = []
    contexts: Counter[str] = Counter()
    for obs in test:
        actual = _standalone_value(obs, source)
        if actual is None:
            continue
        evidence = _evidence(kb, obs, source)
        row: dict[str, Any] = {
            "actual": actual,
            "p50": None,
            "p90": None,
            "benchmark": obs.repo.split(":", 1)[0],
        }
        if evidence is not None:
            row.update(p50=evidence.p50, p90=evidence.p90)
            contexts[f"{evidence.scope}:{evidence.key_kind}"] += 1
        rows.append(row)
    output = {
        "test_rows": len(rows),
        "context_counts": dict(contexts),
        "thresholds": {},
        "by_benchmark": {},
    }
    for threshold in thresholds:
        output["thresholds"][str(threshold)] = {
            "p50": _metrics(rows, threshold=threshold, point="p50"),
            "p90": _metrics(rows, threshold=threshold, point="p90"),
        }
    for benchmark in sorted({row["benchmark"] for row in rows}):
        benchmark_rows = [row for row in rows if row["benchmark"] == benchmark]
        output["by_benchmark"][benchmark] = {
            "test_rows": len(benchmark_rows),
            "thresholds": {
                str(threshold): {
                    "p50": _metrics(benchmark_rows, threshold=threshold, point="p50"),
                    "p90": _metrics(benchmark_rows, threshold=threshold, point="p90"),
                }
                for threshold in thresholds
            },
        }
    return output


def _fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _render_report(report: Mapping[str, Any]) -> str:
    labels = report["labels"]
    lines = [
        "# Heavy-resource threshold validation",
        "",
        "This is a frozen, task-held-out validation of binary rules over legacy clause telemetry.",
        "The RSS result is a diagnostic `sampled_peak_rss_mb` proxy, not a production",
        "cgroup/guest environment-memory prediction.",
        "",
        f"- Dataset: `{report['dataset']['resolved_path']}`",
        f"- Trace files: {report['dataset']['trace_files_seen']}; resource-bearing traces: {report['dataset']['resource_bearing_traces']}",
        f"- Split: seed {report['split']['seed']}, train fraction {report['split']['train_fraction']}, "
        f"{report['split']['train_tasks']} train tasks / {report['split']['test_tasks']} test tasks",
        "- KB rule: exact/prefix/bin backoff; `repo_plus_public` uses only training-task repo history plus training public priors",
        "",
        "## Thresholds",
        "",
        "| Target | Rule threshold | Training labeled | Test labeled | Test heavy fraction |",
        "|---|---:|---:|---:|---:|",
    ]
    for target, label in (("cpu_time_seconds", labels["cpu_time_seconds"]), ("rss_peak_mb", labels["rss_peak_mb"])):
        lines.append(
            f"| {target} | {label['chosen_threshold']} | {label['train_distribution']['labeled']} | "
            f"{label['test_distribution']['labeled']} | {label['test_distribution']['chosen_heavy_fraction']:.1%} |"
        )
    lines += [
        "",
        "## Held-out results",
        "",
        "Metrics are scored on covered rows. `p50` is the normal empirical point estimate; `p90` is a risk-sensitive alternative.",
        "Rows without compatible KB evidence are reported in coverage and are treated as light only in the explicitly named fallback accuracy.",
        "",
        "| Target | Method | Point | Coverage | Heavy rate | All-light acc. | Acc. gain | Balanced acc. | Heavy F1 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for target, method_key in (("cpu_time_seconds", "cpu_time_seconds"), ("rss_peak_mb", "rss_peak_mb")):
        threshold = str(labels[method_key]["chosen_threshold"])
        for method in ("public_only", "repo_plus_public"):
            for point in ("p50", "p90"):
                metric = report["methods"][method][target]["thresholds"][threshold][point]
                lines.append(
                    f"| {target} | {method} | {point} | {_fmt(metric['coverage'])} | "
                    f"{_fmt(metric['heavy_prevalence_on_covered'])} | "
                    f"{_fmt(metric['baseline_all_light_accuracy_on_covered'])} | "
                    f"{_fmt(metric['accuracy_gain_vs_all_light'])} | "
                    f"{_fmt(metric['balanced_accuracy_on_covered'])} | {_fmt(metric['f1_heavy'])} |"
                )
    lines += [
        "",
        "## Threshold sensitivity: repo_plus_public p50",
        "",
        "This table makes the class-imbalance baseline explicit; all-light is a valid no-skill comparator for accuracy only.",
        "",
        "| Target | Threshold | Heavy rate | All-light acc. | Model acc. | Gain | Balanced acc. | Heavy F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for target in ("cpu_time_seconds", "rss_peak_mb"):
        for threshold, values in report["methods"]["repo_plus_public"][target]["thresholds"].items():
            metric = values["p50"]
            lines.append(
                f"| {target} | {threshold} | {_fmt(metric['heavy_prevalence_on_covered'])} | "
                f"{_fmt(metric['baseline_all_light_accuracy_on_covered'])} | "
                f"{_fmt(metric['accuracy_on_covered'])} | {_fmt(metric['accuracy_gain_vs_all_light'])} | "
                f"{_fmt(metric['balanced_accuracy_on_covered'])} | {_fmt(metric['f1_heavy'])} |"
            )
    lines += [
        "",
        "## Interpretation",
        "",
        "- CPU-heavy means at least 1.0 CPU-second of accumulated clause CPU time; this is the rounded training q75.",
        "- RSS-heavy means at least 128 MB of sampled peak RSS; this is close to the training q75 and remains sampling-frequency dependent.",
        "- Accuracy must be compared with the all-light baseline; balanced accuracy and heavy F1 are the primary quality signals here.",
        "- A low coverage result means the current KB backoff has no compatible evidence; it is not evidence that the call is light.",
        "- The 374 traces without resource telemetry are excluded from labels, not counted as negatives.",
        "",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset, traces, dataset_stats = load_dataset(args.dataset)
    train_tasks, test_tasks, groups = _task_split(traces, args.seed, args.train_fraction)
    train_traces = [trace for trace in traces if trace.task_key in train_tasks]
    test_traces = [trace for trace in traces if trace.task_key in test_tasks]
    train_obs = [obs for trace in train_traces for obs in trace.observations]
    test_obs = [obs for trace in test_traces for obs in trace.observations]
    cpu_train = _distribution(train_obs, CPU_SOURCE, CPU_CANDIDATES_S)
    cpu_test = _distribution(test_obs, CPU_SOURCE, CPU_CANDIDATES_S)
    rss_train = _distribution(train_obs, RSS_SOURCE, RSS_CANDIDATES_MB)
    rss_test = _distribution(test_obs, RSS_SOURCE, RSS_CANDIDATES_MB)
    chosen = {
        "cpu_time_seconds": DEFAULT_CPU_THRESHOLD_S,
        "rss_peak_mb": DEFAULT_RSS_THRESHOLD_MB,
    }
    cpu_train_threshold = str(DEFAULT_CPU_THRESHOLD_S)
    rss_train_threshold = str(DEFAULT_RSS_THRESHOLD_MB)
    cpu_test["chosen_heavy_fraction"] = cpu_test["thresholds"][cpu_train_threshold]["heavy_fraction"]
    rss_test["chosen_heavy_fraction"] = rss_test["thresholds"][rss_train_threshold]["heavy_fraction"]
    labels = {
        "cpu_time_seconds": {
            "source": "ClauseObservation.cpu_ns_cumulative / 1e9",
            "chosen_threshold": DEFAULT_CPU_THRESHOLD_S,
            "train_distribution": cpu_train,
            "test_distribution": cpu_test,
        },
        "rss_peak_mb": {
            "source": "ClauseObservation.sampled_peak_rss_mb",
            "chosen_threshold": DEFAULT_RSS_THRESHOLD_MB,
            "warning": "legacy sampled distinct-mm RSS diagnostic; not cgroup/guest environment memory",
            "train_distribution": rss_train,
            "test_distribution": rss_test,
        },
    }
    methods: dict[str, Any] = {}
    for method, include_repo in (("public_only", False), ("repo_plus_public", True)):
        methods[method] = {
            "cpu_time_seconds": _evaluate_method(
                train_obs, test_obs, CPU_SOURCE, CPU_CANDIDATES_S, include_repo=include_repo
            ),
            "rss_peak_mb": _evaluate_method(
                train_obs, test_obs, RSS_SOURCE, RSS_CANDIDATES_MB, include_repo=include_repo
            ),
        }
    report: dict[str, Any] = {
        "schema": "clawtune.heavy_resource_validation.v1",
        "dataset": {
            "requested_path": str(args.dataset.resolve()),
            "resolved_path": str(dataset),
            **dataset_stats,
        },
        "split": {
            "seed": args.seed,
            "train_fraction": args.train_fraction,
            "rule": "clawtune-fixed-name-split-v2",
            "train_tasks": len(train_tasks),
            "test_tasks": len(test_tasks),
            "groups": groups,
        },
        "labels": labels,
        "methods": methods,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime" / "heavy-resource-validation")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-fraction", type=float, default=0.8)
    args = parser.parse_args()
    report = run(args)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    (output / "report.md").write_text(_render_report(report), encoding="utf-8")
    print(_render_report(report))
    print(f"\nWrote {output / 'report.json'}")
    print(f"Wrote {output / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
