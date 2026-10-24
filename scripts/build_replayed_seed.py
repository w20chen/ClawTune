"""Build and validate a cold-start seed from freshly replayed trace-v6 runs.

The source traces are read-only.  All completed tool calls remain available to
training, while the validation report contains only exec/terminal_exec calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/sidecar/src"))

from clawtune_kb import FILES, create_seed, validate_seed
from clawtune_kb.store import digest
from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import predict_call_load
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from clawtune_sidecar.predictors.tool_resource import (
    COLD_START_V2_LABEL_POLICY,
    STRICT_TRACE_LABEL_POLICY,
    TraceLabelPolicy,
    _completed_call_from_tool_span,
    _repo_from_tool_span,
)
from execution_context.duration import SNAPSHOT_NAME, DurationContextKB
from kb_common import DEFAULT_MAX_OPTIONAL_FEATURES
from cold_start.flat_loader import recorded_clause_structure
from tool_resource.commands import normalized_observation
from tool_resource.runtime_kb import (
    LOAD_TARGET_SOURCES,
    PMU_TARGET_SOURCES,
    ClauseObservation,
    ClauseResourceKB,
    RuntimeToolResourceKB,
    ToolCallQuery,
    _clause_value,
    _target_values,
)
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.resource_lattice import LOAD_TARGETS
from tool_resource.sdk import _observations_from_call

from swe_rebench.task_source import infer_repo_from_instance_id

SHELL_TOOLS = frozenset(("exec", "terminal_exec"))
BUCKET_EDGES = (100, 500, 2000, 10000)


@dataclass(frozen=True)
class ReplayAction:
    identity: str
    tool_name: str
    command: str | None
    ts_start: float
    ts_end: float
    call: object
    clauses: tuple
    edge_clauses: tuple


def _trace_version(path: Path) -> int:
    with path.open(encoding="utf-8-sig") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("record_type") == "trace_metadata":
                return int(row.get("trace_format_version", row.get("schema_version", -1)))
    raise ValueError(f"missing trace metadata: {path}")


def _load(task_id: str, path: Path, *, label_policy: TraceLabelPolicy = STRICT_TRACE_LABEL_POLICY):
    from offline.runner import merge_trace_clause_observations

    path = path.resolve(strict=True)
    if _trace_version(path) != 6:
        raise ValueError(f"fresh replay builder requires trace-v6 input: {path}")
    repo = infer_repo_from_instance_id(task_id)
    if not repo:
        raise ValueError(f"cannot infer repository from task id: {task_id}")
    namespace = repo
    starts = {}
    actions = []
    with path.open(encoding="utf-8-sig") as source:
        for line_no, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") != "tool":
                continue
            span_id = record.get("span_id")
            if not isinstance(span_id, str) or not span_id:
                continue
            if record.get("record_type") == "span_start":
                starts[span_id] = record
                continue
            if record.get("record_type") != "span_end":
                continue
            start = starts.get(span_id)
            span_repo = _repo_from_tool_span(
                start, record, fallback_repo=repo, span_id=span_id, line_no=line_no,
            )
            if span_repo != repo:
                raise ValueError(
                    f"{path}:{line_no}: span repository {span_repo!r} does not match {repo!r}"
                )
            call = _completed_call_from_tool_span(start, record, repo=namespace, label_policy=label_policy)
            if call is None:
                continue
            edge_clauses = []
            memory_clauses = []
            telemetry = (record.get("execution") or {}).get("tool_resource") or {}
            call_telemetry = telemetry.get("call_telemetry") or {}
            if call_telemetry.get("clauses") is not None:
                clauses = recorded_clause_structure(
                    call_telemetry.get("command"),
                    call_telemetry.get("clauses"),
                    recorded_only=True,
                )
                edge_clauses = _observations_from_call(
                    namespace,
                    dict(call_telemetry, clauses=list(clauses), censored=call.censored,
                         confirmed_timeout=call.confirmed_timeout),
                    require_timestamps=True,
                    enrich_structure=False,
                )
            for row in (record.get("resources") or {}).get("memory_clause_observations", []):
                if not isinstance(row, dict) or row.get("repo") != span_repo:
                    continue
                values = {field.name: row[field.name] for field in fields(ClauseObservation)
                          if field.name in row}
                values.update(repo=namespace, argv=tuple(values["argv"]),
                              censored=call.censored, confirmed_timeout=call.confirmed_timeout)
                memory_clauses.append(ClauseObservation(**values))
            identity = hashlib.sha256(
                f"{task_id}\0{path.name}\0{span_id}".encode()
            ).hexdigest()
            actions.append(ReplayAction(
                identity=identity,
                tool_name=call.tool_name,
                command=call.command,
                ts_start=call.ts_start,
                ts_end=call.ts_end,
                call=call,
                clauses=tuple(merge_trace_clause_observations(edge_clauses + memory_clauses, label_policy)),
                edge_clauses=tuple(edge_clauses),
            ))
    actions.sort(key=lambda action: (action.ts_start, action.ts_end, action.identity))
    calls = [action.call for action in actions]
    clauses = [row for action in actions for row in action.clauses]
    return repo, SimpleNamespace(actions=actions, calls=calls, clauses=clauses)


def _merge_execution_context(trace_paths: list[Path]) -> DurationContextKB:
    merged = DurationContextKB()
    for trace in trace_paths:
        snapshot = trace.parent / "tool-resource" / SNAPSHOT_NAME
        if not snapshot.is_file():
            continue
        current = DurationContextKB.from_json_obj(json.loads(snapshot.read_text(encoding="utf-8")))
        for sample in current.samples.values():
            merged.observe(
                sample["keys"], ident=sample["id"], task=sample["task"],
                duration_ms=sample["duration_ms"], failure=sample["failure"],
            )
    return merged


def _causal_payloads(tasks, context: DurationContextKB):
    trie = ClauseResourceKB()
    runtime = RuntimeToolResourceKB()
    lattice = LatticeTimeKB(max_optional_features=DEFAULT_MAX_OPTIONAL_FEATURES)
    edge = EdgeKappaRuntime.fit(
        (), BUCKET_EDGES, max_optional_features=DEFAULT_MAX_OPTIONAL_FEATURES,
    )
    edges = load_bucket_edges(BUCKET_EDGES)
    events = []
    for _task_id, _repo, loaded in tasks:
        for action in loaded.actions:
            events.append((action.ts_start, 0, action.identity, action))
            events.append((action.ts_end, 1, action.identity, action))
    for timestamp, phase, identity, action in sorted(events, key=lambda event: event[:3]):
        if phase == 0:
            _prediction, diagnostics = predict_call_load(
                runtime=runtime,
                trie=trie,
                lattice=lattice,
                edge_kappa=edge,
                query=ToolCallQuery(
                    action.call.repo,
                    action.tool_name,
                    action.command,
                    timestamp,
                    memory_measurement=(
                        action.call.memory_measurement
                        or "cgroup_v2_environment_union_v1"
                    ),
                ),
                edges=edges,
                edge_call_id=identity,
            )
            for backend, result in diagnostics.backends.items():
                for target, prediction in result.targets.items():
                    reason = prediction.unavailable_reason or ""
                    if reason.startswith(("backend_error:", "parse_error:")):
                        raise ValueError(f"{identity}: {backend}/{target}: {reason}")
            continue
        for row in action.clauses:
            trie.observe_completed_clause(row)
            lattice.observe_completed_clause(row)
        edge.complete_call(identity, action.edge_clauses, commit_time=timestamp)
        runtime.observe_completed_call(action.call)
    if not events:
        raise ValueError("source traces contain no replayable tool calls")
    horizon = math.nextafter(max(event[0] for event in events), math.inf)
    trie._absorb_completed(horizon)
    runtime._absorb_completed(horizon)
    lattice._advance(horizon)
    edge.kb.commit_before(horizon)
    for kb in (trie, runtime, lattice):
        kb.freeze()
    edge.kb.freeze()
    edge.frozen = True
    payloads = dict(zip(FILES, (trie.to_json_obj(), runtime.to_json_obj(), lattice.to_json_obj())))
    payloads["edge-kappa-kb.json"] = edge.to_snapshot()
    if context.samples:
        payloads[SNAPSHOT_NAME] = context.to_json_obj()
    return payloads


def _holdout(tasks, fraction: float):
    training, test = [], []
    for task_id, _repo, loaded in tasks:
        shell_indexes = [i for i, call in enumerate(loaded.calls)
                         if call.tool_name in SHELL_TOOLS and not call.censored]
        count = max(1, math.ceil(len(shell_indexes) * fraction)) if shell_indexes else 0
        held = set(shell_indexes[-count:])
        training.extend(call for i, call in enumerate(loaded.calls) if i not in held)
        test.extend((task_id, call) for i, call in enumerate(loaded.calls) if i in held)
    return training, test


def _prediction_validation(tasks, fraction: float, minimum: float) -> dict:
    training, test = _holdout(tasks, fraction)
    runtime = RuntimeToolResourceKB()
    for call in training:
        runtime.observe_completed_call(call)
    runtime._absorb_completed(float("inf"))
    runtime.freeze()
    targets = {
        **{name: source for name, source in LOAD_TARGET_SOURCES.items()},
        **{"pmu_" + name: source for name, source in PMU_TARGET_SOURCES.items()},
    }
    stats = {name: Counter(queries=len(test)) for name in targets}
    errors = {name: [] for name in targets}
    actual_totals = Counter()
    for _task_id, call in test:
        query = ToolCallQuery(
            call.repo, call.tool_name, call.command, 1e12,
            memory_measurement=call.memory_measurement or "cgroup_v2_environment_union_v1",
        )
        predictions = runtime.predict_load_samples(query)
        predictions.update({"pmu_" + name: value for name, value in runtime.predict_pmu_samples(query).items()})
        actual = _target_values(call)
        for target, source in targets.items():
            labeled = source in actual
            predicted = target in predictions and bool(predictions[target].get("values"))
            stats[target]["labeled"] += int(labeled)
            stats[target]["predicted"] += int(predicted)
            stats[target]["scored"] += int(labeled and predicted)
            if labeled and predicted:
                values = sorted(float(value) for value in predictions[target]["values"])
                estimate = statistics.median(values)
                errors[target].append(abs(estimate - actual[source]))
                actual_totals[target] += actual[source]
    results = {}
    failed = []
    for target, count in stats.items():
        labeled = count["labeled"]
        coverage = count["scored"] / labeled if labeled else 0.0
        results[target] = {
            **dict(count),
            "labeled_coverage": coverage,
            "mae": statistics.mean(errors[target]) if errors[target] else None,
            "wape": sum(errors[target]) / actual_totals[target]
            if errors[target] and actual_totals[target] else None,
        }
        if not labeled or coverage < minimum:
            failed.append(target)
    return {
        "policy": {
            "scope": "shell calls only; all non-held-out tool calls remain in training",
            "partial_observations": "retained in training; excluded from complete-task MAE/WAPE",
            "holdout": "last shell calls per task",
            "holdout_fraction": fraction,
            "minimum_labeled_coverage": minimum,
            "model": "empty KB plus causal per-repository history; no public-prior fitting",
        },
        "train_calls": len(training),
        "test_shell_calls": len(test),
        "targets": results,
        "passed": not failed,
        "failed_targets": failed,
    }


def _label_policy_provenance(policy: TraceLabelPolicy) -> dict:
    return {
        **asdict(policy),
        "effective_training_policy": "available-or-zero-v1",
        "legacy_parameters": "name and historical recovery flags are compatibility markers, not training filters",
        "observed_values": "retain measured, approximate and partial values with provenance",
        "missing_values": "zero after complementary clause observations are merged",
        "timeout_duration": "configured timeout when available; partial execution remains identified",
        "rss_source": "recorded sampled RSS; zero when missing",
        "environment_memory_source": "recorded environment memory; zero for missing targets",
    }


def build(entries: list[tuple[str, Path]], output: Path, *, holdout_fraction: float,
          minimum_coverage: float, label_policy: TraceLabelPolicy = STRICT_TRACE_LABEL_POLICY) -> dict:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"build into a new directory: {output}")
    loaded_tasks = []
    sources = []
    for task_id, trace in entries:
        trace = trace.resolve(strict=True)
        if output == trace.parent or output.is_relative_to(trace.parent):
            raise ValueError("output must be separate from replay trace directories")
        repo, loaded = _load(task_id, trace, label_policy=label_policy)
        loaded_tasks.append((task_id, repo, loaded))
        sources.append({
            "task_id": task_id,
            "repo": repo,
            "kb_repo": repo,
            "path": f"{task_id}/trace.jsonl",
            "sha256": digest(trace), "calls": len(loaded.calls), "clauses": len(loaded.clauses),
        })
    calls = [call for _task, _repo, loaded in loaded_tasks for call in loaded.calls]
    clauses = [row for _task, _repo, loaded in loaded_tasks for row in loaded.clauses]
    context = _merge_execution_context([path.resolve() for _task, path in entries])
    payloads = _causal_payloads(loaded_tasks, context)
    call_targets = Counter()
    for call in calls:
        call_targets.update(_target_values(call).keys())
    clause_targets = Counter()
    for row in clauses:
        normalized = normalized_observation(row)
        clause_targets.update(
            target for target in LOAD_TARGETS
            if _clause_value(normalized, "load:" + target) is not None
        )
    validation = _prediction_validation(loaded_tasks, holdout_fraction, minimum_coverage)
    provenance = {
        "kind": "fresh-trace-v6-causal-replay",
        "recipe_version": 4,
        "source_policy": "freshly executed trace-v6 runs; no inherited seed samples",
        "replay": "empty KBs; predict at each recorded span start, observe at its completion, preserve recorded event-time order, finalize and freeze after the fourth trace",
        "training_scope": "all completed tool calls; prediction validation reports shell calls only",
        "sources": sources,
        "label_policy": _label_policy_provenance(label_policy),
        "counts": {
            "tasks": len(loaded_tasks), "calls": len(calls), "clauses": len(clauses),
            "partial_calls": sum(call.censored for call in calls),
            "confirmed_timeout_calls": sum(call.confirmed_timeout for call in calls),
            "imputed_clause_rows": sum(bool(row.imputed_targets) for row in clauses),
            "execution_context_samples": len(context.samples),
        },
        "call_target_counts": dict(sorted(call_targets.items())),
        "clause_target_counts": dict(sorted(clause_targets.items())),
        "validation": validation,
    }
    manifest = create_seed(output, payloads, provenance=provenance)
    validate_seed(output)
    for name, loader in (
        ("clause-resource-kb.json", ClauseResourceKB.from_json_obj),
        ("runtime-tool-resource-kb.json", RuntimeToolResourceKB.from_json_obj),
        ("clause-lattice-time-kb.json", LatticeTimeKB.from_json_obj),
    ):
        loader(json.loads((output / name).read_text(encoding="utf-8")))
    EdgeKappaRuntime.from_snapshot(
        json.loads((output / "edge-kappa-kb.json").read_text(encoding="utf-8")), frozen=True,
        max_optional_features=DEFAULT_MAX_OPTIONAL_FEATURES,
    )
    if not validation["passed"]:
        raise RuntimeError("held-out prediction coverage failed: " + ", ".join(validation["failed_targets"]))
    return manifest


def _entry(value: str) -> tuple[str, Path]:
    try:
        task_id, path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected TASK_ID=TRACE_PATH") from exc
    if not task_id or not path:
        raise argparse.ArgumentTypeError("expected TASK_ID=TRACE_PATH")
    return task_id, Path(path)


def build_repository_split(
    dataset: Path,
    split_path: Path,
    output: Path,
    *,
    label_policy: TraceLabelPolicy = STRICT_TRACE_LABEL_POLICY,
    include_test: bool = False,
) -> dict:
    """Export runtime priors; optionally include the held-out tasks for deployment."""
    from clawtune_kb.contracts import validate
    from offline.runner import load_task, _static_clause, _edge_training_events

    dataset, output = dataset.resolve(strict=True), output.resolve()
    if output.exists():
        raise FileExistsError(f"build into a new directory: {output}")
    if output.is_relative_to(dataset) or dataset.is_relative_to(output):
        raise ValueError("seed output must be separate from the read-only dataset")
    split = json.loads(split_path.read_text(encoding="utf-8"))
    validate(split, "offline-split.schema.json")
    if split["rule"] != "sha256-per-benchmark-repository-v1":
        raise ValueError("seed export requires a repository split")
    tasks = split["tasks"]
    train, test = set(split["train"]), set(split["test"])
    if train & test or train | test != set(tasks) or not train or not test:
        raise ValueError("split must partition all tasks into nonempty train/test sets")
    if len({task["benchmark"] for task in tasks.values()}) != 1:
        raise ValueError("build one seed per benchmark")
    train_repos = {tasks[key]["group"] for key in train}
    test_repos = {tasks[key]["group"] for key in test}
    if train_repos & test_repos:
        raise ValueError("test repositories must not participate in seed training")
    calls, clauses, events, sources = [], [], [], []
    strict_call_targets: Counter = Counter()
    strict_clause_targets: Counter = Counter()
    selected_tasks = train | test if include_test else train
    for key in sorted(selected_tasks):
        task = tasks[key]
        if task["grouping"] != "repository" or any(file["version"] != 6 for file in task["files"]):
            raise ValueError("repository seed export requires trace-v6 repository inputs")
        loaded = load_task(
            dataset,
            task,
            "MB",
            recorded_clauses_only=True,
            trace_label_policy=label_policy,
        )
        if label_policy != STRICT_TRACE_LABEL_POLICY:
            strict_loaded = load_task(
                dataset,
                task,
                "MB",
                recorded_clauses_only=True,
                trace_label_policy=STRICT_TRACE_LABEL_POLICY,
            )
            strict_call_targets.update(
                target for call in strict_loaded.calls for target in _target_values(call)
            )
            strict_clause_targets.update(
                target
                for row in strict_loaded.clauses
                for target in LOAD_TARGETS
                if _clause_value(normalized_observation(row), "load:" + target) is not None
            )
        repo = task["group"]
        # Evaluation namespaces isolate datasets. A shipped seed must use the
        # repository identity that ordinary OpenClaw requests actually carry.
        rows = [_static_clause(row, repo) for row in loaded.clauses]
        clauses.extend(rows)
        events.extend(_edge_training_events(rows, key))
        calls.extend(replace(call, repo=repo) for call in loaded.calls)
        sources.append({"task_id": task["task_id"], "repo": repo,
                        "files": task["files"], "calls": len(loaded.calls), "clauses": len(rows)})
    if not calls and not clauses:
        raise ValueError("training repositories contain no usable observations")
    trie = ClauseResourceKB.fit_public(clauses)
    runtime = RuntimeToolResourceKB.fit_public(calls)
    for row in clauses:
        trie.observe_completed_clause(row)
    for call in calls:
        runtime.observe_completed_call(call)
    trie._absorb_completed(float("inf"))
    runtime._absorb_completed(float("inf"))
    trie.freeze()
    runtime.freeze()
    lattice = LatticeTimeKB.fit(clauses, max_optional_features=DEFAULT_MAX_OPTIONAL_FEATURES)
    lattice.freeze()
    edge = EdgeKappaRuntime.fit_events(events, BUCKET_EDGES, frozen=True,
                                     max_optional_features=DEFAULT_MAX_OPTIONAL_FEATURES)
    payloads = dict(zip(FILES, (trie.to_json_obj(), runtime.to_json_obj(), lattice.to_json_obj())))
    payloads["edge-kappa-kb.json"] = edge.to_snapshot()
    call_target_counts = Counter(
        target for call in calls for target in _target_values(call)
    )
    clause_target_counts = Counter(
        target
        for row in clauses
        for target in LOAD_TARGETS
        if _clause_value(normalized_observation(row), "load:" + target) is not None
    )
    provenance = {
        "kind": "repository-split-trace-v6", "recipe_version": 4,
        "split_manifest_sha256": digest(split_path),
        "split_assignment_sha256": split["assignment_sha256"],
        "train_repositories": sorted(train_repos), "test_repositories": sorted(test_repos),
        "sources": sources, "test_updates": 0,
        "source_policy": "training traces only; no inherited seed or test observations",
        "training_scope": "all qualified tool calls; native repository names and public priors",
        "max_optional_features": DEFAULT_MAX_OPTIONAL_FEATURES,
        "counts": {"tasks": len(selected_tasks), "calls": len(calls), "clauses": len(clauses),
                   "partial_calls": sum(call.censored for call in calls),
                   "confirmed_timeout_calls": sum(call.confirmed_timeout for call in calls),
                   "imputed_clause_rows": sum(bool(row.imputed_targets) for row in clauses)},
        "label_policy": _label_policy_provenance(label_policy),
        "call_target_counts": dict(call_target_counts),
        "clause_target_counts": dict(clause_target_counts),
    }
    if include_test:
        provenance.update(
            kind="repository-all-trace-v6",
            source_policy="all tasks from the recorded split; no inherited seed",
            train_repositories=sorted(train_repos | test_repos), test_repositories=[],
            source_split_train_repositories=sorted(train_repos),
            source_split_test_repositories=sorted(test_repos),
            included_original_test_tasks=len(test),
            evaluation_scope="deployment prior; original split is no longer a held-out evaluation",
        )
        provenance.pop("test_updates")
    if label_policy != STRICT_TRACE_LABEL_POLICY:
        provenance["strict_call_target_counts"] = dict(strict_call_targets)
        provenance["strict_clause_target_counts"] = dict(strict_clause_targets)
        provenance["recovered_call_target_counts"] = {
            target: call_target_counts[target] - strict_call_targets[target]
            for target in sorted(call_target_counts | strict_call_targets)
            if call_target_counts[target] != strict_call_targets[target]
        }
        provenance["recovered_clause_target_counts"] = {
            target: clause_target_counts[target] - strict_clause_targets[target]
            for target in sorted(clause_target_counts | strict_clause_targets)
            if clause_target_counts[target] != strict_clause_targets[target]
        }
    return create_seed(output, payloads, provenance=provenance)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", action="append", type=_entry,
                        help="TASK_ID=TRACE_PATH; repeat for each fresh replay")
    parser.add_argument("--dataset", type=Path, help="Trace-v6 dataset used by the offline repository evaluation")
    parser.add_argument("--split", type=Path, help="Its frozen offline split.json; defaults to training repositories only")
    parser.add_argument("--include-test", action="store_true",
                        help="Include all split tasks in a deployment prior; not for held-out evaluation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    parser.add_argument("--minimum-coverage", type=float, default=0.5)
    parser.add_argument(
        "--trace-label-policy",
        choices=("strict", COLD_START_V2_LABEL_POLICY.name),
        default="strict",
        help="Legacy policy marker retained in provenance; both choices use available-or-zero-v1 training",
    )
    args = parser.parse_args()
    if not 0 < args.holdout_fraction < 1:
        parser.error("--holdout-fraction must be between zero and one")
    if not 0 <= args.minimum_coverage <= 1:
        parser.error("--minimum-coverage must be between zero and one")
    label_policy = (
        COLD_START_V2_LABEL_POLICY
        if args.trace_label_policy == COLD_START_V2_LABEL_POLICY.name
        else STRICT_TRACE_LABEL_POLICY
    )
    if args.split or args.dataset:
        if not args.split or not args.dataset or args.trace:
            parser.error("supply --dataset and --split together, without --trace")
        manifest = build_repository_split(
            args.dataset,
            args.split,
            args.output,
            label_policy=label_policy,
            include_test=args.include_test,
        )
    else:
        if args.include_test:
            parser.error("--include-test requires --dataset and --split")
        if not args.trace:
            parser.error("supply --trace or --dataset with --split")
        manifest = build(args.trace, args.output, holdout_fraction=args.holdout_fraction,
                         minimum_coverage=args.minimum_coverage, label_policy=label_policy)
    print(json.dumps(manifest["provenance"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
