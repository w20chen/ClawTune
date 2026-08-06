"""Evaluation engine: train ClawTune's prediction KBs on the training split,
then replay the test split recording a prediction before every tool call.

The engine is a *static* train/test protocol: knowledge bases are built only
from the training split and the test split is never fed back into them.  This
matches the user's "random 80% train / 20% test" request and measures
cross-task (cold-start) generalization.  No algorithm code is modified; the
engine only constructs the algorithm input objects and drives the public KB
APIs (``ClauseResourceKB``, ``LatticeTimeKB``, ``RuntimeToolResourceKB``).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from legacy_eval._bootstrap import ensure_paths

ensure_paths()

from tool_resource.runtime_kb import (  # noqa: E402
    ClauseObservation,
    ClauseResourceKB,
    CompletedCall,
    LatencyBuckets,
    RuntimeToolResourceKB,
    ToolCallQuery,
)
from tool_time.command import shell_command_heads  # noqa: E402
from tool_time.lattice_kb import LATTICE_TIME_ALGORITHMS, LatticeTimeKB  # noqa: E402

from legacy_eval.loader import (  # noqa: E402
    ClauseEvent,
    TaskArtifacts,
    ToolCallEvent,
)
from legacy_eval.metrics import (  # noqa: E402
    summarize_bucket,
    summarize_point,
    summarize_quantile,
)
from legacy_eval.split import split_tasks  # noqa: E402

# Evaluation track names, one per algorithm (or target for the continuous KB).
BUCKET_TRACK = "clause_latency_bucket"
LATTICE_TRACKS = tuple(LATTICE_TIME_ALGORITHMS)  # shrinkage, loso, max_cardinality
CONTINUOUS_LATENCY_TRACK = "continuous_latency_p90"
CONTINUOUS_CPU_TRACK = "continuous_cpu_p90"

TRACKS = (
    (BUCKET_TRACK,)
    + LATTICE_TRACKS
    + (CONTINUOUS_LATENCY_TRACK, CONTINUOUS_CPU_TRACK)
)


@dataclass(frozen=True)
class EvalConfig:
    """Configuration for one evaluation run."""

    train_frac: float = 0.8
    seed: int = 42
    bucket_edges_ms: tuple[float, ...] = (100.0, 500.0, 2_000.0, 10_000.0)
    max_train_tasks: int | None = None  # smoke-test cap
    max_test_tasks: int | None = None  # smoke-test cap


@dataclass
class EvalResult:
    """One evaluation run's full outcome (serializable)."""

    config: EvalConfig
    dataset_dir: str
    all_task_ids: list[str]
    train_ids: list[str]
    test_ids: list[str]
    counts: dict[str, Any]
    records: dict[str, list[dict[str, Any]]]
    summaries: dict[str, dict[str, Any]]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json_obj(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "dataset_dir": self.dataset_dir,
            "all_task_ids": self.all_task_ids,
            "train_ids": self.train_ids,
            "test_ids": self.test_ids,
            "counts": self.counts,
            "records": self.records,
            "summaries": self.summaries,
            "meta": self.meta,
        }


# ---------------------------------------------------------------------------
# Algorithm input construction
# ---------------------------------------------------------------------------


def to_clause_observation(event: ClauseEvent) -> ClauseObservation:
    """Build the exact object ``ClauseResourceKB``/``LatticeTimeKB`` consume.

    Legacy clause rows carry no timestamps; ClawTune's own Stage-2 loader
    synthesises ``ts_start=0.0, ts_end=latency/1000`` for the same input, so
    we mirror that here (timestamps are irrelevant to public fitting).
    """

    latency = max(0.0, event.latency_ms)
    return ClauseObservation(
        repo=event.repo,
        bin=event.bin,
        argv=event.argv,
        ts_start=0.0,
        ts_end=latency / 1000.0,
        latency_ms=latency,
        peak_cpu_cores=event.peak_cpu_cores,
        sampled_peak_rss_mb=event.sampled_peak_rss_mb,
        cpu_ns_cumulative=None,
        in_loop=False,
        in_pipe=False,
        in_subst=False,
        pipeline_position=-1,
    )


def to_completed_call(call: ToolCallEvent) -> CompletedCall:
    """Build the exact object ``RuntimeToolResourceKB`` consumes.

    The continuous latency target is derived by the KB as
    ``(ts_end - ts_start) * 1000``, so the synthetic timestamps are chosen to
    reproduce the observed call duration exactly.
    """

    duration_ms = call.duration_ms
    if duration_ms is None or duration_ms < 0.0:
        duration_ms = max(0.0, (call.ts_end - call.ts_start) * 1000.0)
    return CompletedCall(
        repo=call.repo,
        tool_name=call.tool_name,
        command=call.command,
        ts_start=0.0,
        ts_end=duration_ms / 1000.0,
        censored=False,
        peak_cpu_cores=call.peak_cpu_cores,
        peak_cpu_cores_eligible=call.peak_cpu_cores is not None,
        peak_memory_mb=None,
        peak_memory_mb_eligible=False,
        ambient_before_mb=None,
    )


def _single_head(command: str | None) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return None
    heads = shell_command_heads(command)
    return heads[0] if len(heads) == 1 else None


def _public_node_keys(tool_name: str, command: str | None) -> list[tuple[str, str]]:
    """Mirror of ``RuntimeToolResourceKB._public_keys``: head, tool, global."""

    keys: list[tuple[str, str]] = []
    head = _single_head(command)
    if head is not None:
        keys.append(("binary_head", head))
    keys.append(("tool_name", tool_name))
    keys.append(("global", ""))
    return keys


def build_runtime_public(calls: Sequence[CompletedCall]) -> RuntimeToolResourceKB:
    """Build the frozen continuous public layer from training calls.

    Tries the canonical ``RuntimeToolResourceKB.fit_public``; because the
    legacy format carries no ambient-memory anchor, the ``peak_memory_mb``
    target can never be fit, so on the expected ``ValueError`` the engine
    builds public nodes for exactly the targets the data supports
    (``latency_ms`` and ``peak_cpu_cores``), mirroring ``fit_public``'s
    accumulation.  Predictions for the memory target remain unavailable
    (the KB returns an unavailable prediction when the query has no ambient
    anchor), which is the honest result for this dataset.
    """

    calls = list(calls)
    if not calls:
        return RuntimeToolResourceKB()
    try:
        return RuntimeToolResourceKB.fit_public(calls)
    except ValueError:
        accumulator: dict[str, dict[tuple[str, str], list[float]]] = {
            "latency_ms": {},
            "peak_cpu_cores": {},
        }
        for call in calls:
            keys = _public_node_keys(call.tool_name, call.command)
            if not call.censored:
                latency = (call.ts_end - call.ts_start) * 1000.0
                for key in keys:
                    accumulator["latency_ms"].setdefault(key, []).append(latency)
            if call.peak_cpu_cores_eligible and call.peak_cpu_cores is not None:
                for key in keys:
                    accumulator["peak_cpu_cores"].setdefault(key, []).append(
                        float(call.peak_cpu_cores)
                    )
        if not accumulator["latency_ms"].get(("global", "")):
            raise ValueError(
                "legacy corpus has no eligible latency labels for the "
                "continuous predictor"
            )
        kb = RuntimeToolResourceKB()
        kb._public = {  # type: ignore[attr-defined]
            target: {
                key: tuple(values)
                for key, values in nodes.items()
            }
            for target, nodes in accumulator.items()
        }
        kb._public["peak_memory_mb"] = {}  # type: ignore[attr-defined]
        return kb


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------


def _bucket_record(
    repo: str,
    event: ClauseEvent,
    buckets: LatencyBuckets,
    *,
    predicted: Any,
    reason: str | None = None,
) -> dict[str, Any]:
    actual_bucket = buckets.bucket_id(event.latency_ms)
    if predicted is None:
        return {
            "repo": repo,
            "bin": event.bin,
            "argv": list(event.argv),
            "actual_ms": event.latency_ms,
            "actual_bucket": actual_bucket,
            "predicted_bucket": None,
            "probability_by_bucket": None,
            "unavailable_reason": reason,
            "evidence_count": 0,
            "scope": None,
            "key_kind": None,
        }
    return {
        "repo": repo,
        "bin": event.bin,
        "argv": list(event.argv),
        "actual_ms": event.latency_ms,
        "actual_bucket": actual_bucket,
        "predicted_bucket": predicted.bucket_id,
        "probability_by_bucket": list(predicted.probability_by_bucket),
        "unavailable_reason": None,
        "evidence_count": predicted.evidence_count,
        "scope": predicted.scope,
        "key_kind": predicted.key_kind,
    }


def _lattice_record(
    repo: str,
    event: ClauseEvent,
    algorithm: str,
    *,
    predicted_ms: float | None,
    reason: str | None,
    evidence_count: int = 0,
    exact_match: bool | None = None,
) -> dict[str, Any]:
    return {
        "repo": repo,
        "bin": event.bin,
        "argv": list(event.argv),
        "algorithm": algorithm,
        "actual_ms": event.latency_ms,
        "predicted_ms": predicted_ms,
        "unavailable_reason": reason,
        "evidence_count": evidence_count,
        "exact_match": exact_match,
    }


def _continuous_record(
    repo: str,
    call: ToolCallEvent,
    target: str,
    *,
    actual: float | None,
    predicted: float | None,
    reason: str | None,
    evidence_count: int = 0,
    scope: str | None = None,
    key_kind: str | None = None,
) -> dict[str, Any]:
    return {
        "repo": repo,
        "tool_name": call.tool_name,
        "command": call.command,
        "target": target,
        "actual": actual,
        "predicted": predicted,
        "unavailable_reason": reason,
        "evidence_count": evidence_count,
        "scope": scope,
        "key_kind": key_kind,
    }


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _replay_test(
    tasks: Mapping[str, TaskArtifacts],
    test_ids: Sequence[str],
    *,
    clause_kb: ClauseResourceKB,
    lattice_kb: LatticeTimeKB,
    runtime_kb: RuntimeToolResourceKB,
    buckets: LatencyBuckets,
) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {track: [] for track in TRACKS}
    # One shared monotonic clock so every causal KB guard sees non-decreasing
    # query timestamps across the whole replay (pure static: nothing is
    # observed during the test, so the value is only an ordering key).
    clock = itertools.count(start=1)

    for task_id in test_ids:
        task = tasks[task_id]
        for event in task.clause_events:
            ts = float(next(clock))
            # Latency-bucket predictor (clause level).
            try:
                prediction = clause_kb.predict_clause_latency_bucket(
                    task_id,
                    event.bin,
                    event.argv,
                    buckets,
                )
            except (ValueError, TypeError) as exc:
                records[BUCKET_TRACK].append(
                    _bucket_record(
                        task_id,
                        event,
                        buckets,
                        predicted=None,
                        reason=str(exc),
                    )
                )
            else:
                records[BUCKET_TRACK].append(
                    _bucket_record(task_id, event, buckets, predicted=prediction)
                )
            # Lattice point predictors (shrinkage / loso / max_cardinality).
            try:
                outcomes = lattice_kb.predict_clauses(
                    task_id,
                    [{"bin": event.bin, "argv": list(event.argv)}],
                    ts,
                    shell_command=True,
                )
            except (ValueError, TypeError) as exc:
                for algorithm in LATTICE_TRACKS:
                    records[algorithm].append(
                        _lattice_record(
                            task_id,
                            event,
                            algorithm,
                            predicted_ms=None,
                            reason=f"lattice_error:{type(exc).__name__}",
                        )
                    )
                continue
            if not outcomes:
                for algorithm in LATTICE_TRACKS:
                    records[algorithm].append(
                        _lattice_record(
                            task_id,
                            event,
                            algorithm,
                            predicted_ms=None,
                            reason="no_executable_clause",
                        )
                    )
                continue
            by_algorithm = {
                prediction.algorithm: prediction
                for prediction in outcomes[0].predictions
            }
            for algorithm in LATTICE_TRACKS:
                prediction = by_algorithm.get(algorithm)
                if prediction is None:
                    records[algorithm].append(
                        _lattice_record(
                            task_id,
                            event,
                            algorithm,
                            predicted_ms=None,
                            reason="missing_prediction",
                        )
                    )
                elif prediction.prediction_ms is None:
                    records[algorithm].append(
                        _lattice_record(
                            task_id,
                            event,
                            algorithm,
                            predicted_ms=None,
                            reason=prediction.unavailable_reason,
                        )
                    )
                else:
                    records[algorithm].append(
                        _lattice_record(
                            task_id,
                            event,
                            algorithm,
                            predicted_ms=prediction.prediction_ms,
                            reason=None,
                            evidence_count=prediction.evidence_count,
                            exact_match=prediction.exact_match,
                        )
                    )

        # Continuous conditional-p90 (call level), chronological order.
        ordered_calls = sorted(
            task.tool_calls, key=lambda call: (call.ts_start, call.ts_end)
        )
        for call in ordered_calls:
            ts = float(next(clock))
            query = ToolCallQuery(
                repo=task_id,
                tool_name=call.tool_name,
                command=call.command,
                ts_start=ts,
                ambient_before_mb=None,
            )
            try:
                predictions = runtime_kb.query(query)
            except (ValueError, TypeError) as exc:
                for target, track in (
                    ("latency_ms", CONTINUOUS_LATENCY_TRACK),
                    ("peak_cpu_cores", CONTINUOUS_CPU_TRACK),
                ):
                    records[track].append(
                        _continuous_record(
                            task_id,
                            call,
                            target,
                            actual=call.duration_ms
                            if target == "latency_ms"
                            else call.peak_cpu_cores,
                            predicted=None,
                            reason=f"continuous_error:{type(exc).__name__}",
                        )
                    )
                continue
            latency_target = predictions.get("latency_ms")
            cpu_target = predictions.get("peak_cpu_cores")
            records[CONTINUOUS_LATENCY_TRACK].append(
                _continuous_record(
                    task_id,
                    call,
                    "latency_ms",
                    actual=call.duration_ms,
                    predicted=(
                        latency_target.conditional_p90
                        if latency_target is not None
                        else None
                    ),
                    reason=(
                        latency_target.note
                        if latency_target is not None
                        else "no_latency_target"
                    ),
                    evidence_count=(
                        latency_target.evidence_count
                        if latency_target is not None
                        else 0
                    ),
                    scope=latency_target.scope if latency_target is not None else None,
                    key_kind=(
                        latency_target.key_kind
                        if latency_target is not None
                        else None
                    ),
                )
            )
            records[CONTINUOUS_CPU_TRACK].append(
                _continuous_record(
                    task_id,
                    call,
                    "peak_cpu_cores",
                    actual=call.peak_cpu_cores,
                    predicted=(
                        cpu_target.conditional_p90 if cpu_target is not None else None
                    ),
                    reason=(
                        cpu_target.note if cpu_target is not None else "no_cpu_target"
                    ),
                    evidence_count=(
                        cpu_target.evidence_count if cpu_target is not None else 0
                    ),
                    scope=cpu_target.scope if cpu_target is not None else None,
                    key_kind=cpu_target.key_kind if cpu_target is not None else None,
                )
            )
    return records


# ---------------------------------------------------------------------------
# Top-level evaluation
# ---------------------------------------------------------------------------


def _collect_counts(
    tasks: Mapping[str, TaskArtifacts],
    train_ids: Sequence[str],
    test_ids: Sequence[str],
) -> dict[str, Any]:
    train_clause = 0
    train_clause_eligible = 0
    train_calls = 0
    test_clause = 0
    test_calls = 0
    for task_id, task in tasks.items():
        if task_id in train_ids:
            train_clause += len(task.clause_events)
            train_clause_eligible += sum(1 for ev in task.clause_events if ev.eligible)
            train_calls += sum(
                1
                for call in task.tool_calls
                if call.success and call.duration_ms is not None
            )
        elif task_id in test_ids:
            test_clause += len(task.clause_events)
            test_calls += len(task.tool_calls)
    return {
        "train_tasks": len(train_ids),
        "test_tasks": len(test_ids),
        "train_clause_observations": train_clause,
        "train_clause_observations_eligible": train_clause_eligible,
        "train_tool_calls_success": train_calls,
        "test_clause_events": test_clause,
        "test_tool_calls": test_calls,
    }


def split_train_test(
    tasks: Mapping[str, TaskArtifacts],
    config: EvalConfig,
) -> tuple[list[str], list[str], list[ClauseObservation], list[CompletedCall]]:
    """Return ``(train_ids, test_ids, train_clause, train_calls)`` for a config.

    Training observations use the same production-consistent eligibility
    filter as ClawTune's own cold start (eligible clause telemetry + successful
    tool calls with a measured duration).
    """

    task_ids = sorted(tasks)
    train_ids, test_ids = split_tasks(
        task_ids,
        train_frac=config.train_frac,
        seed=config.seed,
    )
    if config.max_train_tasks is not None:
        train_ids = train_ids[: config.max_train_tasks]
    if config.max_test_tasks is not None:
        test_ids = test_ids[: config.max_test_tasks]
    train_clause: list[ClauseObservation] = []
    train_calls: list[CompletedCall] = []
    for task_id in train_ids:
        task = tasks[task_id]
        for event in task.clause_events:
            if event.eligible:
                train_clause.append(to_clause_observation(event))
        for call in task.tool_calls:
            if call.success and call.duration_ms is not None and call.duration_ms >= 0.0:
                train_calls.append(to_completed_call(call))
    return train_ids, test_ids, train_clause, train_calls


def build_kbs(
    train_clause: Sequence[ClauseObservation],
    train_calls: Sequence[CompletedCall],
) -> tuple[ClauseResourceKB, LatticeTimeKB, RuntimeToolResourceKB, str | None]:
    """Train the three prediction KBs on training observations only.

    ``evaluate`` and the cold-start exporter share this exact code path, so an
    exported KB is always the same KB the evaluation measured.
    """

    try:
        clause_kb = ClauseResourceKB.fit_public(train_clause)
    except ValueError as exc:
        clause_kb = ClauseResourceKB()
        clause_fit_error = str(exc)
    else:
        clause_fit_error = None

    lattice_kb = LatticeTimeKB.fit(train_clause)
    lattice_kb.prepare()

    runtime_kb = build_runtime_public(train_calls)
    return clause_kb, lattice_kb, runtime_kb, clause_fit_error


def evaluate(
    tasks: Mapping[str, TaskArtifacts],
    *,
    dataset_dir: str = "",
    config: EvalConfig | None = None,
) -> EvalResult:
    """Run the full static train/test evaluation over loaded tasks."""

    if config is None:
        config = EvalConfig()
    train_ids, test_ids, train_clause, train_calls = split_train_test(
        tasks, config
    )
    buckets = LatencyBuckets(config.bucket_edges_ms)

    clause_kb, lattice_kb, runtime_kb, clause_fit_error = build_kbs(
        train_clause, train_calls
    )

    # --- Test replay (20% split, predict-only) ---------------------------
    records = _replay_test(
        tasks,
        test_ids,
        clause_kb=clause_kb,
        lattice_kb=lattice_kb,
        runtime_kb=runtime_kb,
        buckets=buckets,
    )

    summaries = {
        BUCKET_TRACK: summarize_bucket(records[BUCKET_TRACK]),
        **{
            track: summarize_point(records[track])
            for track in LATTICE_TRACKS
        },
        CONTINUOUS_LATENCY_TRACK: summarize_quantile(
            records[CONTINUOUS_LATENCY_TRACK]
        ),
        CONTINUOUS_CPU_TRACK: summarize_quantile(records[CONTINUOUS_CPU_TRACK]),
    }

    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol": "static_train_test",
        "algorithm_family": {
            "clause_latency_bucket": "ClauseResourceKB",
            "lattice_*": "LatticeTimeKB",
            "continuous_*": "RuntimeToolResourceKB",
        },
        "notes": {
            "clause_fit_error": clause_fit_error,
            "continuous_memory_unavailable": (
                "legacy format has no ambient memory anchor; peak_memory_mb "
                "target not evaluated"
            ),
            "ts": (
                "legacy clause rows carry no timestamps; synthetic ordering "
                "used for the causal guards (static protocol observes nothing)"
            ),
        },
    }
    return EvalResult(
        config=config,
        dataset_dir=dataset_dir,
        all_task_ids=sorted(tasks),
        train_ids=train_ids,
        test_ids=test_ids,
        counts=_collect_counts(tasks, train_ids, test_ids),
        records=records,
        summaries=summaries,
        meta=meta,
    )


def evaluate_dataset(
    dataset_dir: str | Path,
    *,
    config: EvalConfig | None = None,
) -> EvalResult:
    """Convenience: load every task under *dataset_dir*, then evaluate."""

    from legacy_eval.loader import load_all

    tasks = load_all(dataset_dir)
    return evaluate(
        tasks,
        dataset_dir=str(dataset_dir),
        config=config,
    )


def write_json_report(result: EvalResult, path: str | Path) -> Path:
    """Write the full serializable evaluation result to *path*."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_json_obj(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "BUCKET_TRACK",
    "CONTINUOUS_CPU_TRACK",
    "CONTINUOUS_LATENCY_TRACK",
    "EvalConfig",
    "EvalResult",
    "LATTICE_TRACKS",
    "TRACKS",
    "build_kbs",
    "build_runtime_public",
    "evaluate",
    "evaluate_dataset",
    "split_train_test",
    "to_clause_observation",
    "to_completed_call",
    "write_json_report",
]
