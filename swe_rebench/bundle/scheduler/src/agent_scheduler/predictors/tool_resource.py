from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import shlex
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_scheduler.contracts.models import (
    ToolBeforeRequest,
    ToolCompletedEvent,
    ToolPrediction,
)
from agent_scheduler.identity import (
    CorrelationKey,
    correlation_key,
    owners_compatible,
)
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from agent_scheduler.tool_resource_commands import extract_command
from tool_resource.features import (
    parse_command_clauses,
    shell_bin_requires_exec_evidence,
)
from tool_resource.metrics import ecdf_quantile
from tool_resource.runtime_kb import (
    ClauseLatencyBucketOutcome,
    ClauseLatencyBucketPrediction,
    ClauseObservation,
    ClauseResourceKB,
    CommandLatencyBucketPrediction,
    CompletedCall,
    LatencyBuckets,
    RuntimeToolResourceKB,
    TargetPrediction,
    ToolCallQuery,
)
from tool_resource.sdk import (
    CommandRun,
    DockerExecutionContext,
    ToolResourceSDK,
    _load_valid_artifact,
    _observations_from_call,
)
from tool_time.lattice_kb import (
    LATTICE_TIME_ALGORITHMS,
    ClauseLatticeTimePredictions,
    LatticeTimeKB,
    LatticeTimePrediction,
)


_SHARED_RESOURCE_ATTRIBUTION_SOURCES = frozenset(
    {"shared-runtime-process", "shared-sandbox-container"}
)
_SHARED_RESOURCE_SCOPE_SOURCES = frozenset(
    {"openclaw-runtime", "openclaw-sandbox"}
)


@dataclass(frozen=True)
class ToolResourceLoadReport:
    stage2_traces_seen: int
    stage2_traces_loaded: int
    openclaw_traces_seen: int
    openclaw_traces_accepted: int
    openclaw_tool_spans_seen: int
    observations_loaded: int
    continuous_observations_loaded: int
    kb_available: bool
    continuous_kb_available: bool
    lattice_observations_loaded: int
    lattice_kb_available: bool
    rejections: tuple[str, ...]


@dataclass(frozen=True)
class OpenClawTraceLoadReport:
    traces_seen: int
    traces_accepted: int
    tool_spans_seen: int
    observations_loaded: int
    rejections: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionTelemetrySummary:
    execution_id: str
    tool_call_id: str | None
    artifact_path: str | None
    started: bool
    status: str
    unavailable_reason: str | None = None
    kb_observations_added: int = 0
    kb_update_error: str | None = None
    call_telemetry: dict[str, Any] | None = None
    artifact_summary: dict[str, Any] | None = None


class KnowledgeBaseFlushError(RuntimeError):
    """Raised when queued shared-KB state cannot be made durable."""


@dataclass(frozen=True)
class _KnowledgeBaseUpdate:
    lattice_observations: tuple[ClauseObservation, ...] = ()


@dataclass(frozen=True)
class _KnowledgeBaseBarrier:
    future: Future[None]


class _KnowledgeBaseWriteCoordinator:
    """Single-writer, batched persistence coordinator for one shared KB.

    Caller threads update the cheap in-memory runtime/clause indexes under the
    predictor lock, then enqueue a durability notification.  The worker owns
    lattice preparation and all snapshot writes, coalescing bursts from many
    sessions into one generation.  A barrier always retries dirty snapshots
    before it resolves, so a sidecar drain can make all preceding observations
    durable without racing another completion writer.
    """

    def __init__(
        self,
        apply_batch: Callable[[tuple[ClauseObservation, ...]], None],
        *,
        batch_window_s: float = 0.01,
        max_batch_size: int = 512,
        idle_timeout_s: float = 0.25,
    ) -> None:
        self._apply_batch = apply_batch
        self._batch_window_s = batch_window_s
        self._max_batch_size = max_batch_size
        self._idle_timeout_s = idle_timeout_s
        self._queue: queue.Queue[_KnowledgeBaseUpdate | _KnowledgeBaseBarrier] = (
            queue.Queue()
        )
        self._state_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._inflight = False

    def enqueue(
        self,
        lattice_observations: Sequence[ClauseObservation] = (),
    ) -> None:
        self._queue.put(
            _KnowledgeBaseUpdate(tuple(lattice_observations))
        )
        self._ensure_worker()

    def flush(self, timeout_seconds: float | None = None) -> None:
        barrier: Future[None] = Future()
        self._queue.put(_KnowledgeBaseBarrier(barrier))
        self._ensure_worker()
        try:
            barrier.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            raise TimeoutError(
                "timed out waiting for shared tool-resource KB flush"
            ) from exc

    def pending(self) -> bool:
        with self._state_lock:
            return self._inflight or not self._queue.empty()

    def _ensure_worker(self) -> None:
        with self._state_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run,
                name="clawtune-kb-writer",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=self._idle_timeout_s)
            except queue.Empty:
                with self._state_lock:
                    if self._queue.empty():
                        self._worker = None
                        return
                continue

            with self._state_lock:
                self._inflight = True
            items: list[_KnowledgeBaseUpdate | _KnowledgeBaseBarrier] = [first]
            if not isinstance(first, _KnowledgeBaseBarrier):
                deadline = time.monotonic() + self._batch_window_s
                while len(items) < self._max_batch_size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        item = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    items.append(item)
                    if isinstance(item, _KnowledgeBaseBarrier):
                        break

            try:
                self._process(items)
            finally:
                with self._state_lock:
                    self._inflight = False

    def _process(
        self,
        items: Sequence[_KnowledgeBaseUpdate | _KnowledgeBaseBarrier],
    ) -> None:
        observations: list[ClauseObservation] = []
        barriers: list[Future[None]] = []
        for item in items:
            if isinstance(item, _KnowledgeBaseBarrier):
                barriers.append(item.future)
            else:
                observations.extend(item.lattice_observations)

        try:
            # Even an empty barrier batch retries snapshots left dirty by a
            # prior transient persistence error.
            self._apply_batch(tuple(observations))
        except BaseException as exc:
            for barrier in barriers:
                if not barrier.done():
                    barrier.set_exception(exc)
            return
        for barrier in barriers:
            if not barrier.done():
                barrier.set_result(None)


class _DeferredSdkClauseKb:
    """SDK view that predicts from the live KB but defers all mutations."""

    def __init__(self, predictor: "ToolResourcePredictor") -> None:
        self._predictor = predictor

    def predict_command_latency_bucket(self, *args: Any, **kwargs: Any) -> Any:
        with self._predictor._kb_lock:
            return self._predictor.kb.predict_command_latency_bucket(
                *args,
                **kwargs,
            )

    def observe_completed_clause(self, _observation: ClauseObservation) -> None:
        # ToolResourceSDK returns the validated observations to the adapter.
        # The predictor applies them once under its shared-KB lock.
        return None


class ToolResourcePredictor:
    """OpenClaw adapter for the vendored tool_resource SDK and KB."""

    def __init__(
        self,
        *,
        kb: ClauseResourceKB,
        buckets: LatencyBuckets,
        report: ToolResourceLoadReport,
        repo: str,
        artifact_dir: Path | None = None,
        container_executable: str = "docker",
        clause_kb_snapshot_path: Path | None = None,
        runtime_kb_snapshot_path: Path | None = None,
        lattice_kb: LatticeTimeKB | None = None,
        lattice_kb_snapshot_path: Path | None = None,
    ) -> None:
        self.kb = kb
        self.continuous_kb = RuntimeToolResourceKB()
        self.buckets = buckets
        self.report = report
        self.repo = repo
        self.artifact_dir = artifact_dir
        self.container_executable = container_executable
        self.clause_kb_snapshot_path = clause_kb_snapshot_path
        self.runtime_kb_snapshot_path = runtime_kb_snapshot_path
        self.lattice_kb = lattice_kb or LatticeTimeKB()
        self.lattice_kb_snapshot_path = lattice_kb_snapshot_path
        self._kb_lock = threading.RLock()
        self._execution_lock = threading.RLock()
        self._execution_start_lock = threading.Lock()
        self._starts_lock = threading.RLock()
        self._runtime_kb_version = 0
        self._runtime_kb_persisted_version = 0
        self._clause_kb_version = 0
        self._clause_kb_persisted_version = 0
        self._lattice_kb_version = 0
        self._lattice_kb_persisted_version = 0
        self._lattice_needs_prepare = False
        self._sdk = ToolResourceSDK(  # type: ignore[arg-type]
            _DeferredSdkClauseKb(self),
            buckets,
        )
        self._runs_by_execution_id: dict[str, CommandRun] = {}
        self._execution_owners: dict[str, tuple[str | None, str | None]] = {}
        self._telemetry_by_execution_id: dict[str, ExecutionTelemetrySummary] = {}
        self._starts: dict[CorrelationKey, ToolBeforeRequest] = {}
        self._kb_writes = _KnowledgeBaseWriteCoordinator(
            self._flush_kb_batch,
        )

    @classmethod
    def from_traces(
        cls,
        *,
        openclaw_trace_paths: Iterable[Path],
        stage2_trace_paths: Iterable[Path],
        buckets: LatencyBuckets,
        repo: str = "openclaw",
        artifact_dir: Path | None = None,
        container_executable: str = "docker",
    ) -> "ToolResourcePredictor":
        openclaw_paths = list(_expand_trace_paths(openclaw_trace_paths))
        stage2_paths = list(_expand_trace_paths(stage2_trace_paths))
        observations: list[ClauseObservation] = []
        continuous_observations: list[CompletedCall] = []
        rejections: list[str] = []
        stage2_loaded = 0

        for path in stage2_paths:
            try:
                artifact = _read_stage2_artifact(path)
                observations.extend(_stage2_observations(artifact, fallback_repo=repo))
                stage2_loaded += 1
            except ValueError as exc:
                rejections.append(f"{path}: {exc}")

        openclaw_accepted = 0
        openclaw_spans_seen = 0
        for path in openclaw_paths:
            try:
                loaded = load_openclaw_trace_observations(path, repo=repo)
            except ValueError as exc:
                rejections.append(f"{path}: {exc}")
                continue
            openclaw_accepted += 1
            openclaw_spans_seen += loaded.tool_spans_seen
            continuous_observations.extend(loaded.completed_calls)

        snapshot_path = _clause_kb_snapshot_path(artifact_dir)
        runtime_snapshot_path = _runtime_kb_snapshot_path(artifact_dir)
        lattice_snapshot_path = _lattice_kb_snapshot_path(artifact_dir)
        kb: ClauseResourceKB
        kb_has_public_evidence = False
        loaded_snapshot = _load_clause_kb_snapshot(snapshot_path, rejections)
        if loaded_snapshot is not None:
            kb = loaded_snapshot
            if observations and not _has_public_clause_latency(kb):
                try:
                    kb._public = ClauseResourceKB.fit_public(observations)._public  # type: ignore[attr-defined]
                except ValueError as exc:
                    rejections.append(f"fit_public: {exc}")
            for observation in observations:
                kb.observe_completed_clause(observation)
            kb_has_public_evidence = _has_public_clause_latency(kb)
        elif observations:
            try:
                kb = ClauseResourceKB.fit_public(observations)
                kb_has_public_evidence = True
            except ValueError as exc:
                rejections.append(f"fit_public: {exc}")
                kb = ClauseResourceKB()
            for observation in observations:
                kb.observe_completed_clause(observation)
        else:
            kb = ClauseResourceKB()

        loaded_lattice_snapshot = _load_lattice_kb_snapshot(
            lattice_snapshot_path,
            rejections,
        )
        if loaded_lattice_snapshot is None:
            lattice_kb = LatticeTimeKB.fit(observations)
        else:
            lattice_kb = loaded_lattice_snapshot
            lattice_kb.merge_historical(observations)
        lattice_kb_available = lattice_kb.observation_count > 0
        try:
            lattice_kb.prepare()
        except Exception as exc:
            lattice_kb_available = False
            rejections.append(f"prepare lattice KB: {type(exc).__name__}: {exc}")

        predictor = cls(
            kb=kb,
            buckets=buckets,
            report=ToolResourceLoadReport(
                stage2_traces_seen=len(stage2_paths),
                stage2_traces_loaded=stage2_loaded,
                openclaw_traces_seen=len(openclaw_paths),
                openclaw_traces_accepted=openclaw_accepted,
                openclaw_tool_spans_seen=openclaw_spans_seen,
                observations_loaded=len(observations),
                continuous_observations_loaded=len(continuous_observations),
                kb_available=kb_has_public_evidence,
                continuous_kb_available=bool(continuous_observations),
                lattice_observations_loaded=lattice_kb.observation_count,
                lattice_kb_available=lattice_kb_available,
                rejections=tuple(rejections),
            ),
            repo=repo,
            artifact_dir=artifact_dir,
            container_executable=container_executable,
            clause_kb_snapshot_path=snapshot_path,
            runtime_kb_snapshot_path=runtime_snapshot_path,
            lattice_kb=lattice_kb,
            lattice_kb_snapshot_path=lattice_snapshot_path,
        )
        loaded_runtime_snapshot = _load_runtime_kb_snapshot(
            runtime_snapshot_path,
            rejections,
        )
        if loaded_runtime_snapshot is not None:
            predictor.continuous_kb = loaded_runtime_snapshot
        for call in continuous_observations:
            predictor.continuous_kb.observe_completed_call(call)
        if observations or loaded_snapshot is not None:
            predictor._persist_clause_kb()
        if continuous_observations or loaded_runtime_snapshot is not None:
            predictor._persist_runtime_kb()
        if observations or loaded_lattice_snapshot is not None:
            predictor._persist_lattice_kb()
        return predictor

    @classmethod
    def from_openclaw_traces(
        cls,
        trace_paths: Iterable[Path],
        *,
        buckets: LatencyBuckets,
        repo: str = "openclaw",
    ) -> "ToolResourcePredictor":
        return cls.from_traces(
            openclaw_trace_paths=trace_paths,
            stage2_trace_paths=(),
            buckets=buckets,
            repo=repo,
        )

    async def predict(
        self,
        request: ToolBeforeRequest,
        *,
        ambient_before_mb: float | None = None,
    ) -> ToolPrediction:
        command = _command_for_request(request)
        repo = request.repo or self.repo
        query_ts = time.time()
        lattice_time_predictions: tuple[ClauseLatticeTimePredictions, ...] = ()
        try:
            clauses, parse_failed = clauses_from_tool_request(
                request.tool_name,
                request.raw_params,
            )
            if request.tool_name == "exec":
                lattice_time_predictions = self._lattice_predictions_for_clauses(
                    clauses,
                    query_ts,
                    repo=repo,
                    parse_failed=parse_failed,
                    shell_command=True,
                )
            with self._kb_lock:
                prediction = self.kb.predict_command_latency_bucket_from_clauses(
                    repo,
                    clauses,
                    query_ts,
                    self.buckets,
                    command=command or request.tool_name,
                    parse_failed=parse_failed,
                    shell_command=request.tool_name == "exec",
                )
        except Exception as exc:
            continuous_predictions = self._continuous_predictions_for_request(
                request,
                command,
                query_ts,
                ambient_before_mb=ambient_before_mb,
            )
            runtime_p50_ms, runtime_p90_ms = self._continuous_latency_topline_ms(
                request,
                command,
                query_ts,
            )
            return ToolPrediction(
                duration_p50_ms=runtime_p50_ms,
                duration_p90_ms=runtime_p90_ms,
                resource_class=_resource_class_for_duration_ms(runtime_p90_ms),
                tool_resource=_tool_resource_prediction_payload(
                    _unavailable_prediction_for_request(
                        request,
                        repo=repo,
                        command=command,
                        reason=_prediction_error_reason(exc),
                    ),
                    continuous_predictions=continuous_predictions,
                    lattice_time_predictions=lattice_time_predictions,
                ),
            )
        continuous_predictions = self._continuous_predictions_for_request(
            request,
            command,
            query_ts,
            ambient_before_mb=ambient_before_mb,
        )
        if prediction.prediction is None:
            runtime_p50_ms, runtime_p90_ms = self._continuous_latency_topline_ms(
                request,
                command,
                query_ts,
            )
            return ToolPrediction(
                duration_p50_ms=runtime_p50_ms,
                duration_p90_ms=runtime_p90_ms,
                resource_class=_resource_class_for_duration_ms(runtime_p90_ms),
                tool_resource=_tool_resource_prediction_payload(
                    prediction,
                    continuous_predictions=continuous_predictions,
                    lattice_time_predictions=lattice_time_predictions,
                ),
            )

        bucket_prediction = prediction.prediction
        runtime_p50_ms, runtime_p90_ms = self._continuous_latency_topline_ms(
            request,
            command,
            query_ts,
        )
        duration_p50_ms = _bucket_percentile_ms(
            bucket_prediction.probability_by_bucket,
            self.buckets,
            0.5,
        )
        duration_p90_ms = _bucket_percentile_ms(
            bucket_prediction.probability_by_bucket,
            self.buckets,
            0.9,
        )
        return ToolPrediction(
            duration_p50_ms=runtime_p50_ms if runtime_p50_ms is not None else duration_p50_ms,
            duration_p90_ms=runtime_p90_ms if runtime_p90_ms is not None else duration_p90_ms,
            resource_class=(
                _resource_class_for_duration_ms(runtime_p90_ms)
                if runtime_p90_ms is not None
                else _resource_class_for_bucket(bucket_prediction.bucket_id)
            ),
            confidence=max(bucket_prediction.probability_by_bucket, default=0.0),
            tool_resource=_tool_resource_prediction_payload(
                prediction,
                continuous_predictions=continuous_predictions,
                lattice_time_predictions=lattice_time_predictions,
            ),
        )

    def record_tool_started(self, request: ToolBeforeRequest) -> None:
        with self._starts_lock:
            self._starts[correlation_key(request)] = request

    def observe_completion(
        self,
        event: ToolCompletedEvent,
        sample: ToolRuntimeSample,
    ) -> int:
        with self._starts_lock:
            start = self._starts.pop(correlation_key(event), None)
            if start is None and event.tool_call_id is not None:
                start = self._pop_start_by_tool_call_id(
                    event.tool_call_id,
                    event,
                )
        completed_call = completed_call_from_completion(
            event,
            sample,
            repo=event.repo or (start.repo if start is not None else None) or self.repo,
            start=start,
        )
        if completed_call is not None:
            with self._kb_lock:
                self.continuous_kb.observe_completed_call(completed_call)
                self._runtime_kb_version += 1
            if self.runtime_kb_snapshot_path is not None:
                self._kb_writes.enqueue()
        return 1 if completed_call is not None else 0

    def begin_execution(
        self,
        *,
        execution_id: str,
        tool_call_id: str | None,
        command: str,
        container_id: str | None,
        repo: str | None = None,
        gateway_id: str | None = None,
        runtime_id: str | None = None,
        cgroup_path: str | None = None,
        trusted_root_pid: int | None = None,
    ) -> bool:
        reused = self._reuse_active_execution(
            execution_id,
            gateway_id=gateway_id,
            runtime_id=runtime_id,
            trusted_root_pid=trusted_root_pid,
        )
        if reused is not None:
            return reused
        previous = self._telemetry_by_execution_id.get(execution_id)
        if previous is not None and previous.started:
            return previous.status != "unavailable"
        host_scope_available = bool(cgroup_path and trusted_root_pid is not None)
        if self.artifact_dir is None or (not container_id and not host_scope_available):
            reason = (
                "artifact_dir_unconfigured"
                if self.artifact_dir is None
                else "execution_scope_unavailable"
            )
            self._telemetry_by_execution_id[execution_id] = ExecutionTelemetrySummary(
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                artifact_path=None,
                started=False,
                status="unavailable",
                unavailable_reason=reason,
            )
            self._trim_execution_telemetry()
            return False
        artifact_path = self.artifact_dir / f"{_safe_artifact_name(execution_id)}.json"
        context = DockerExecutionContext(
            container_id=container_id,
            container_executable=self.container_executable,
            repo=repo or self.repo,
            artifact_path=artifact_path,
            cgroup_path=cgroup_path,
            trusted_root_pid=trusted_root_pid,
        )
        with self._execution_start_lock:
            reused = self._reuse_active_execution(
                execution_id,
                gateway_id=gateway_id,
                runtime_id=runtime_id,
                trusted_root_pid=trusted_root_pid,
            )
            if reused is not None:
                return reused
            try:
                run = self._sdk.start_command(
                    context,
                    tool_call_id or execution_id,
                    command,
                )
            except Exception as exc:
                self._telemetry_by_execution_id[
                    execution_id
                ] = ExecutionTelemetrySummary(
                    execution_id=execution_id,
                    tool_call_id=tool_call_id,
                    artifact_path=str(artifact_path),
                    started=False,
                    status="unavailable",
                    unavailable_reason=f"start_failed:{type(exc).__name__}: {exc}",
                )
                self._trim_execution_telemetry()
                return False
            observer = getattr(run, "_observer", None)
            telemetry_available = bool(
                getattr(observer, "telemetry_available", True)
            )
            unavailable_reason = (
                getattr(observer, "unavailable_reason", None)
                if not telemetry_available
                else None
            )
            with self._execution_lock:
                self._runs_by_execution_id[execution_id] = run
                self._execution_owners[execution_id] = (gateway_id, runtime_id)
        self._telemetry_by_execution_id[execution_id] = ExecutionTelemetrySummary(
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            artifact_path=str(artifact_path),
            started=True,
            status="started" if telemetry_available else "unavailable",
            unavailable_reason=unavailable_reason,
        )
        self._trim_execution_telemetry()
        return telemetry_available

    def _reuse_active_execution(
        self,
        execution_id: str,
        *,
        gateway_id: str | None,
        runtime_id: str | None,
        trusted_root_pid: int | None,
    ) -> bool | None:
        with self._execution_lock:
            existing = self._runs_by_execution_id.get(execution_id)
            existing_owner = self._execution_owners.get(execution_id)
            if existing is None:
                return None
            if existing_owner is not None and not _execution_owners_compatible(
                existing_owner,
                (gateway_id, runtime_id),
            ):
                raise ValueError("active execution owner changed")
            observer = getattr(existing, "_observer", None)
            if trusted_root_pid is not None:
                bind_root = getattr(observer, "bind_trusted_root", None)
                if callable(bind_root):
                    bind_root(trusted_root_pid)
            if existing_owner is not None:
                self._execution_owners[execution_id] = (
                    existing_owner[0] or gateway_id,
                    existing_owner[1] or runtime_id,
                )
            return bool(getattr(observer, "telemetry_available", True))

    def finish_execution(
        self,
        *,
        execution_id: str,
        exit_code: int | None,
        signal: int | None,
        raw_result: Any | None = None,
        succeeded: bool | None = None,
        incomplete_reason: str | None = None,
    ) -> ExecutionTelemetrySummary:
        if incomplete_reason is not None and (
            exit_code is not None or signal is not None
        ):
            raise ValueError(
                "incomplete Stage-2 finalization cannot carry launcher exit status"
            )
        with self._execution_lock:
            run = self._runs_by_execution_id.pop(execution_id, None)
            self._execution_owners.pop(execution_id, None)
        if run is None:
            return self._telemetry_by_execution_id.get(
                execution_id,
                ExecutionTelemetrySummary(
                    execution_id=execution_id,
                    tool_call_id=None,
                    artifact_path=None,
                    started=False,
                    status="unavailable",
                    unavailable_reason="no_active_stage2_run",
                ),
            )
        replay_execution = (
            "incomplete"
            if incomplete_reason is not None
            else ("completed" if exit_code == 0 and signal is None else "failed")
        )
        workload_result = _stage2_workload_result(
            raw_result,
            exit_code=exit_code,
            signal=signal,
            # A terminal OpenClaw hook is not an authoritative process exit.
            # Do not copy its optimistic success bit into an orphaned run.
            succeeded=None if incomplete_reason is not None else succeeded,
        )
        try:
            result = self._sdk.finish_command(
                run,
                workload_result,
                replay_execution=replay_execution,
            )
        except Exception as exc:
            finish_error = f"finish_failed:{type(exc).__name__}: {exc}"
            summary = ExecutionTelemetrySummary(
                execution_id=execution_id,
                tool_call_id=run.tool_call_id,
                artifact_path=str(run._observer.context.artifact_path),
                started=True,
                status="unavailable",
                unavailable_reason=incomplete_reason or finish_error,
                kb_update_error=(
                    finish_error if incomplete_reason is not None else None
                ),
            )
            self._telemetry_by_execution_id[execution_id] = summary
            self._trim_execution_telemetry()
            return summary
        kb_update_errors = (
            [result.kb_update_error] if result.kb_update_error is not None else []
        )
        accepted_observations: list[ClauseObservation] = []
        if result.kb_observations_added:
            with self._kb_lock:
                for observation in result.kb_observations:
                    try:
                        self.kb.observe_completed_clause(observation)
                    except Exception as exc:
                        kb_update_errors.append(
                            "clause_kb_update_failed:"
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
                    accepted_observations.append(observation)
                    self._clause_kb_version += 1
            if accepted_observations:
                self._kb_writes.enqueue(accepted_observations)
        kb_update_error = "; ".join(kb_update_errors) or None
        call_telemetry = (
            dict(result.call_telemetry)
            if isinstance(result.call_telemetry, dict)
            else None
        )
        summary = ExecutionTelemetrySummary(
            execution_id=execution_id,
            tool_call_id=run.tool_call_id,
            artifact_path=str(run._observer.context.artifact_path),
            started=True,
            status=(
                "unavailable"
                if incomplete_reason is not None
                else _stage2_status(
                    result.telemetry_artifact,
                    call_telemetry,
                    kb_update_error,
                )
            ),
            unavailable_reason=(
                incomplete_reason
                if incomplete_reason is not None
                else _stage2_unavailable_reason(
                    result.telemetry_artifact,
                    call_telemetry,
                    kb_update_error,
                )
            ),
            kb_observations_added=len(accepted_observations),
            kb_update_error=kb_update_error,
            call_telemetry=_compact_call_telemetry(call_telemetry),
            artifact_summary=_compact_artifact_summary(result.telemetry_artifact),
        )
        self._telemetry_by_execution_id[execution_id] = summary
        self._trim_execution_telemetry()
        return summary

    def execution_telemetry(self, execution_id: str) -> ExecutionTelemetrySummary | None:
        return self._telemetry_by_execution_id.get(execution_id)

    def execution_active(self, execution_id: str) -> bool:
        """Return whether this execution still owns an unfinished SDK run."""

        with self._execution_lock:
            return execution_id in self._runs_by_execution_id

    def active_execution_ids(
        self,
        runtime_id: str,
        gateway_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return active collectors owned by one runtime, including orphans."""

        with self._execution_lock:
            return tuple(
                sorted(
                    execution_id
                    for execution_id in self._runs_by_execution_id
                    if _execution_owner_matches_runtime(
                        self._execution_owners.get(execution_id),
                        runtime_id=runtime_id,
                        gateway_id=gateway_id,
                    )
                )
            )

    def _trim_execution_telemetry(self) -> None:
        while len(self._telemetry_by_execution_id) > 10_000:
            oldest = next(iter(self._telemetry_by_execution_id))
            self._telemetry_by_execution_id.pop(oldest, None)

    def flush_kb_updates(self, timeout_seconds: float | None = None) -> None:
        """Wait until every previously queued shared-KB update is durable.

        The sidecar drain endpoint should run this blocking barrier in a worker
        thread.  A raised error means at least one in-memory generation is
        still newer than its snapshot and the runtime must not be declared
        drained yet.
        """

        self._kb_writes.flush(timeout_seconds)

    def close(self) -> None:
        """Make queued KB generations durable before sidecar shutdown."""

        self.flush_kb_updates(timeout_seconds=30.0)

    def kb_updates_pending(self) -> bool:
        """Return whether the single writer has queued or in-flight work."""

        return self._kb_writes.pending()

    def _flush_kb_batch(
        self,
        lattice_observations: tuple[ClauseObservation, ...],
    ) -> None:
        errors: list[str] = []
        snapshots: list[tuple[str, Path, dict[str, Any], int]] = []

        with self._kb_lock:
            for observation in lattice_observations:
                try:
                    self.lattice_kb.observe_completed_clause(observation)
                except Exception as exc:
                    errors.append(
                        "lattice_kb_update_failed:"
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                self._lattice_kb_version += 1
                self._lattice_needs_prepare = True

            if self._lattice_needs_prepare:
                try:
                    self.lattice_kb.prepare()
                except Exception as exc:
                    errors.append(
                        f"lattice_prepare_failed:{type(exc).__name__}: {exc}"
                    )
                else:
                    self._lattice_needs_prepare = False

            self._capture_kb_snapshot_locked(
                snapshots,
                errors,
                name="runtime",
                path=self.runtime_kb_snapshot_path,
                version=self._runtime_kb_version,
                persisted_version=self._runtime_kb_persisted_version,
                serializer=self.continuous_kb.to_json_obj,
            )
            self._capture_kb_snapshot_locked(
                snapshots,
                errors,
                name="clause",
                path=self.clause_kb_snapshot_path,
                version=self._clause_kb_version,
                persisted_version=self._clause_kb_persisted_version,
                serializer=self.kb.to_json_obj,
            )
            if not self._lattice_needs_prepare:
                self._capture_kb_snapshot_locked(
                    snapshots,
                    errors,
                    name="lattice",
                    path=self.lattice_kb_snapshot_path,
                    version=self._lattice_kb_version,
                    persisted_version=self._lattice_kb_persisted_version,
                    serializer=self.lattice_kb.to_json_obj,
                )

        for name, path, payload, version in snapshots:
            try:
                _write_json_atomic(path, payload)
            except Exception as exc:
                errors.append(
                    f"{name}_kb_persist_failed:{type(exc).__name__}: {exc}"
                )
                continue
            with self._kb_lock:
                if name == "runtime":
                    self._runtime_kb_persisted_version = max(
                        self._runtime_kb_persisted_version,
                        version,
                    )
                elif name == "clause":
                    self._clause_kb_persisted_version = max(
                        self._clause_kb_persisted_version,
                        version,
                    )
                else:
                    self._lattice_kb_persisted_version = max(
                        self._lattice_kb_persisted_version,
                        version,
                    )

        if errors:
            raise KnowledgeBaseFlushError("; ".join(errors))

    def _capture_kb_snapshot_locked(
        self,
        snapshots: list[tuple[str, Path, dict[str, Any], int]],
        errors: list[str],
        *,
        name: str,
        path: Path | None,
        version: int,
        persisted_version: int,
        serializer: Callable[[], dict[str, Any]],
    ) -> None:
        if path is None or version <= persisted_version:
            return
        try:
            payload = serializer()
        except Exception as exc:
            errors.append(
                f"{name}_kb_snapshot_failed:{type(exc).__name__}: {exc}"
            )
            return
        snapshots.append((name, path, payload, version))

    def _persist_clause_kb(self) -> bool:
        if self.clause_kb_snapshot_path is None:
            return False
        try:
            _write_json_atomic(self.clause_kb_snapshot_path, self.kb.to_json_obj())
            return True
        except Exception:
            return False

    def _persist_runtime_kb(self) -> bool:
        if self.runtime_kb_snapshot_path is None:
            return False
        try:
            _write_json_atomic(self.runtime_kb_snapshot_path, self.continuous_kb.to_json_obj())
            return True
        except Exception:
            return False

    def _persist_lattice_kb(self) -> bool:
        if self.lattice_kb_snapshot_path is None:
            return False
        try:
            _write_json_atomic(
                self.lattice_kb_snapshot_path,
                self.lattice_kb.to_json_obj(),
            )
            return True
        except Exception:
            return False

    def _lattice_predictions_for_clauses(
        self,
        clauses: Sequence[Mapping[str, Any]],
        query_ts: float,
        *,
        repo: str,
        parse_failed: bool,
        shell_command: bool,
    ) -> tuple[ClauseLatticeTimePredictions, ...]:
        try:
            with self._kb_lock:
                return self.lattice_kb.predict_clauses(
                    repo,
                    clauses,
                    query_ts,
                    parse_failed=parse_failed,
                    shell_command=shell_command,
                )
        except Exception as exc:
            return _unavailable_lattice_time_predictions(
                clauses,
                reason=f"lattice_prediction_error:{type(exc).__name__}",
                shell_command=shell_command,
            )

    def _continuous_predictions_for_request(
        self,
        request: ToolBeforeRequest,
        command: str | None,
        ts_start: float,
        ambient_before_mb: float | None = None,
    ) -> dict[str, Any]:
        repo = request.repo or self.repo
        query = ToolCallQuery(
            repo=repo,
            tool_name=request.tool_name,
            command=command,
            ts_start=ts_start,
            ambient_before_mb=ambient_before_mb,
        )
        with self._kb_lock:
            return self._continuous_predictions_for_query(query)

    def _continuous_predictions_for_query(
        self,
        query: ToolCallQuery,
    ) -> dict[str, Any]:
        predictions: dict[str, Any] = {}
        try:
            self.continuous_kb._advance(query.ts_start)  # type: ignore[attr-defined]
        except AttributeError:
            # Historical RuntimeToolResourceKB exposes absorption through query().
            try:
                self.continuous_kb.query(query)
            except Exception:
                pass
        except Exception:
            pass
        for target in ("latency_ms", "peak_cpu_cores", "peak_memory_mb"):
            try:
                prediction = self.continuous_kb._predict_target(query, target)  # type: ignore[attr-defined]
            except Exception as exc:
                predictions[target] = {
                    "target": target,
                    "conditional_p90": None,
                    "scope": None,
                    "key_kind": None,
                    "evidence_count": 0,
                    "fallback_path": [],
                    "note": _continuous_unavailable_note(target, exc),
                }
                continue
            predictions[target] = _target_prediction_payload(prediction)
        return predictions

    def _continuous_latency_topline_ms(
        self,
        request: ToolBeforeRequest,
        command: str | None,
        ts_start: float,
    ) -> tuple[int | None, int | None]:
        repo = request.repo or self.repo
        query = ToolCallQuery(
            repo=repo,
            tool_name=request.tool_name,
            command=command,
            ts_start=ts_start,
        )
        with self._kb_lock:
            return self._continuous_latency_for_query(query)

    def _continuous_latency_for_query(
        self,
        query: ToolCallQuery,
    ) -> tuple[int | None, int | None]:
        try:
            self.continuous_kb._advance(query.ts_start)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                self.continuous_kb.query(query)
            except Exception:
                pass
        except Exception:
            pass
        try:
            values, _scope, _kind, _path = self.continuous_kb._select(  # type: ignore[attr-defined]
                query.repo,
                "latency_ms",
                query.tool_name,
                query.command,
            )
        except Exception:
            return None, None
        if not values:
            return None, None
        return (
            max(0, int(round(ecdf_quantile(values, 0.5)))),
            max(0, int(round(ecdf_quantile(values, 0.9)))),
        )

    def _pop_start_by_tool_call_id(
        self,
        tool_call_id: str,
        completion: ToolCompletedEvent,
    ) -> ToolBeforeRequest | None:
        matches = [
            (key, request)
            for key, request in self._starts.items()
            if request.tool_call_id == tool_call_id
            and owners_compatible(request, completion)
        ]
        if len(matches) != 1:
            return None
        key, request = matches[0]
        self._starts.pop(key, None)
        return request


@dataclass(frozen=True)
class _LoadedTrace:
    tool_spans_seen: int
    observations: tuple[ClauseObservation, ...]
    completed_calls: tuple[CompletedCall, ...]


def load_openclaw_trace_observations(path: Path, *, repo: str) -> _LoadedTrace:
    if not path.is_file():
        raise ValueError("trace path is not a file")
    starts: dict[str, dict[str, Any]] = {}
    observations: list[ClauseObservation] = []
    completed_calls: list[CompletedCall] = []
    tool_spans_seen = 0
    with path.open(encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_no}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict) or record.get("kind") != "tool":
                continue
            span_id = record.get("span_id")
            if not isinstance(span_id, str) or not span_id:
                continue
            if record.get("record_type") == "span_start":
                starts[span_id] = record
                continue
            if record.get("record_type") != "span_end":
                continue
            tool_spans_seen += 1
            start = starts.get(span_id)
            span_repo = _repo_from_tool_span(
                start,
                record,
                fallback_repo=repo,
                span_id=span_id,
                line_no=line_no,
            )
            completed_call = _completed_call_from_tool_span(
                start,
                record,
                repo=span_repo,
            )
            if completed_call is not None:
                completed_calls.append(completed_call)
    return _LoadedTrace(tool_spans_seen, tuple(observations), tuple(completed_calls))


def _repo_from_tool_span(
    start: dict[str, Any] | None,
    end: dict[str, Any],
    *,
    fallback_repo: str,
    span_id: str,
    line_no: int,
) -> str:
    """Resolve one span's repo without silently merging tenant histories."""

    explicit: list[tuple[str, str]] = []
    for record_type, record in (("span_start", start), ("span_end", end)):
        if record is None or "repo" not in record or record["repo"] is None:
            continue
        value = record["repo"]
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"line {line_no}: tool span {span_id!r} has invalid "
                f"{record_type} repo"
            )
        explicit.append((record_type, value))

    if len(explicit) == 2 and explicit[0][1] != explicit[1][1]:
        raise ValueError(
            f"line {line_no}: tool span {span_id!r} repo mismatch: "
            f"span_start={explicit[0][1]!r}, span_end={explicit[1][1]!r}"
        )
    return explicit[0][1] if explicit else fallback_repo


def observation_from_completion(
    event: ToolCompletedEvent,
    sample: ToolRuntimeSample,
    *,
    repo: str,
    start: ToolBeforeRequest | None = None,
) -> ClauseObservation | None:
    if not event.succeeded:
        return None
    raw_params = start.raw_params if start is not None else _raw_params_from_result(event.raw_event, event.raw_result)
    clauses, parse_failed = clauses_from_tool_request(event.tool_name, raw_params)
    if parse_failed or len(clauses) != 1:
        return None
    clause = clauses[0]
    ts_start = sample.started_at
    ts_end = sample.ended_at
    if ts_end < ts_start:
        ts_end = ts_start
    shared_resources = _completion_uses_shared_resources(event, start)
    return ClauseObservation(
        repo=repo,
        bin=str(clause["bin"]),
        argv=tuple(str(item) for item in clause["argv"]),
        ts_start=ts_start,
        ts_end=ts_end,
        latency_ms=max(0.0, float(event.duration_ms)),
        peak_cpu_cores=(
            None if shared_resources else sample.cpu_utilization_avg_cores
        ),
        sampled_peak_rss_mb=(
            None if shared_resources else _rss_mb(sample.rss_bytes_peak)
        ),
        cpu_ns_cumulative=(
            None if shared_resources else _cpu_ns(sample.cpu_time_delta_s)
        ),
        in_loop=False,
        in_pipe=False,
        in_subst=False,
        pipeline_position=-1,
    )


def completed_call_from_completion(
    event: ToolCompletedEvent,
    sample: ToolRuntimeSample,
    *,
    repo: str,
    start: ToolBeforeRequest | None = None,
) -> CompletedCall | None:
    if not event.succeeded:
        return None
    raw_params = start.raw_params if start is not None else _raw_params_from_result(event.raw_event, event.raw_result)
    command = extract_command(raw_params)
    ts_start = sample.started_at
    ts_end = sample.ended_at
    if ts_end < ts_start:
        ts_end = ts_start
    peak_memory_mb = _rss_mb(sample.rss_bytes_peak)
    ambient_before_mb = _rss_mb(sample.rss_bytes_before)
    shared_resources = _completion_uses_shared_resources(event, start)
    return CompletedCall(
        repo=repo,
        tool_name=event.tool_name,
        command=command,
        ts_start=ts_start,
        ts_end=ts_end,
        censored=False,
        peak_cpu_cores=sample.cpu_utilization_avg_cores,
        peak_cpu_cores_eligible=(
            not shared_resources and sample.cpu_utilization_avg_cores is not None
        ),
        peak_memory_mb=peak_memory_mb,
        peak_memory_mb_eligible=(
            not shared_resources
            and peak_memory_mb is not None
            and ambient_before_mb is not None
        ),
        ambient_before_mb=ambient_before_mb,
    )


def clauses_from_tool_request(tool_name: str, raw_params: Any) -> tuple[tuple[dict[str, Any], ...], bool]:
    command = extract_command(raw_params)
    if tool_name == "exec" and command:
        return _clauses_from_command(command)
    if not tool_name:
        return (), False
    argv = [tool_name]
    if isinstance(raw_params, dict):
        hint = raw_params.get("path") or raw_params.get("operation") or raw_params.get("cmd")
        if isinstance(hint, str) and hint:
            argv.append(Path(hint).name if "/" in hint or "\\" in hint else hint)
    return ({"bin": tool_name, "argv": argv},), False


def _observation_from_tool_span(
    start: dict[str, Any] | None,
    end: dict[str, Any],
    *,
    repo: str,
) -> ClauseObservation | None:
    status = end.get("status") if isinstance(end.get("status"), dict) else {}
    if status.get("code") not in {"ok", "unknown"}:
        return None
    tool_name = end.get("name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    raw_params = _raw_params_from_start(start)
    clauses, parse_failed = clauses_from_tool_request(tool_name, raw_params)
    if parse_failed or len(clauses) != 1:
        return None
    clause = clauses[0]
    duration_ms = _duration_ms(end)
    if duration_ms is None or duration_ms <= 0:
        return None
    ts_start, ts_end = _span_times(end, duration_ms)
    resources = end.get("resources") if isinstance(end.get("resources"), dict) else {}
    execution = end.get("execution") if isinstance(end.get("execution"), dict) else {}
    shared_resources = _uses_shared_resources(resources) or _uses_shared_resources(
        execution
    )
    return ClauseObservation(
        repo=repo,
        bin=str(clause["bin"]),
        argv=tuple(str(item) for item in clause["argv"]),
        ts_start=ts_start,
        ts_end=ts_end,
        latency_ms=duration_ms,
        peak_cpu_cores=(
            None
            if shared_resources
            else _optional_float(resources.get("cpu_utilization_avg_cores"))
        ),
        sampled_peak_rss_mb=(
            None if shared_resources else _rss_mb(resources.get("rss_peak_bytes"))
        ),
        cpu_ns_cumulative=(
            None if shared_resources else _cpu_ns(resources.get("cpu_time_s"))
        ),
        in_loop=False,
        in_pipe=False,
        in_subst=False,
        pipeline_position=-1,
    )


def _completed_call_from_tool_span(
    start: dict[str, Any] | None,
    end: dict[str, Any],
    *,
    repo: str,
) -> CompletedCall | None:
    status = end.get("status") if isinstance(end.get("status"), dict) else {}
    if status.get("code") not in {"ok", "unknown"}:
        return None
    tool_name = end.get("name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    duration_ms = _duration_ms(end)
    if duration_ms is None or duration_ms < 0:
        return None
    ts_start, ts_end = _span_times(end, duration_ms)
    raw_params = _raw_params_from_start(start)
    command = extract_command(raw_params)
    resources = end.get("resources") if isinstance(end.get("resources"), dict) else {}
    peak_memory_mb = _rss_mb(resources.get("rss_peak_bytes"))
    ambient_before_mb = _rss_mb(resources.get("memory_rss_bytes_before"))
    peak_cpu_cores = _optional_float(resources.get("cpu_utilization_avg_cores"))
    execution = end.get("execution") if isinstance(end.get("execution"), dict) else {}
    shared_resources = _uses_shared_resources(resources) or _uses_shared_resources(
        execution
    )
    return CompletedCall(
        repo=repo,
        tool_name=tool_name,
        command=command,
        ts_start=ts_start,
        ts_end=ts_end,
        censored=False,
        peak_cpu_cores=peak_cpu_cores,
        peak_cpu_cores_eligible=not shared_resources and peak_cpu_cores is not None,
        peak_memory_mb=peak_memory_mb,
        peak_memory_mb_eligible=(
            not shared_resources
            and peak_memory_mb is not None
            and ambient_before_mb is not None
        ),
        ambient_before_mb=ambient_before_mb,
    )


def _completion_uses_shared_resources(
    event: ToolCompletedEvent,
    start: ToolBeforeRequest | None,
) -> bool:
    # The completion scope is the final, server-resolved attribution scope.
    # Fall back to the request only when completion did not resolve one.
    scope = event.resource_scope
    if scope is None and start is not None:
        scope = start.resource_scope
    return _uses_shared_resources(scope)


def _uses_shared_resources(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        attribution_source = value.get("attribution_source")
        source = value.get("source")
    else:
        attribution_source = getattr(value, "attribution_source", None)
        source = getattr(value, "source", None)
    return (
        attribution_source in _SHARED_RESOURCE_ATTRIBUTION_SOURCES
        or source in _SHARED_RESOURCE_SCOPE_SOURCES
    )


def _read_stage2_artifact(path: Path) -> dict[str, Any]:
    return _load_valid_artifact(path)


def _stage2_observations(
    artifact: dict[str, Any],
    *,
    fallback_repo: str,
) -> list[ClauseObservation]:
    provenance = artifact.get("provenance")
    repo = (
        provenance.get("repo")
        if isinstance(provenance, dict) and isinstance(provenance.get("repo"), str)
        else fallback_repo
    )
    observations: list[ClauseObservation] = []
    for call in artifact.get("calls", []):
        if not isinstance(call, dict) or call.get("eligible_for_kb") is not True:
            continue
        observations.extend(
            _observations_from_call(
                repo,
                call,
                require_timestamps=False,
            )
        )
    return observations


def _stage2_completed_calls(
    artifact: dict[str, Any],
    *,
    fallback_repo: str,
) -> list[CompletedCall]:
    provenance = artifact.get("provenance")
    repo = (
        provenance.get("repo")
        if isinstance(provenance, dict) and isinstance(provenance.get("repo"), str)
        else fallback_repo
    )
    calls: list[CompletedCall] = []
    for call in artifact.get("calls", []):
        if not isinstance(call, dict) or call.get("eligible_for_kb") is not True:
            continue
        clauses = call.get("clauses")
        if not isinstance(clauses, list):
            continue
        for clause in clauses:
            completed = _stage2_completed_call(repo, clause)
            if completed is not None:
                calls.append(completed)
    return calls


def _stage2_completed_call(repo: str, row: Any) -> CompletedCall | None:
    if not isinstance(row, dict):
        return None
    availability = row.get("availability")
    if not isinstance(availability, dict) or availability.get("latency") != "ok":
        return None
    latency_ms = _optional_float(row.get("latency_ms"))
    if latency_ms is None:
        return None
    ts_start = _optional_float(row.get("ts_start"))
    ts_end = _optional_float(row.get("ts_end"))
    if ts_start is None or ts_end is None or ts_end < ts_start:
        ts_start = 0.0
        ts_end = latency_ms / 1000
    argv = row.get("argv")
    command = shlex.join(argv) if isinstance(argv, list) and all(isinstance(item, str) for item in argv) else None
    peak_cpu_cores = _optional_float(row.get("peak_cpu_cores"))
    return CompletedCall(
        repo=repo,
        tool_name="exec",
        command=command,
        ts_start=ts_start,
        ts_end=ts_end,
        censored=False,
        peak_cpu_cores=peak_cpu_cores,
        peak_cpu_cores_eligible=peak_cpu_cores is not None,
        peak_memory_mb=None,
        peak_memory_mb_eligible=False,
        ambient_before_mb=None,
    )


def _stage2_workload_result(
    raw_result: Any,
    *,
    exit_code: int | None,
    signal: int | None,
    succeeded: bool | None,
) -> dict[str, Any]:
    """Build the bounded live-result envelope consumed by Stage-2.

    The launcher supplies the authoritative exit status, while OpenClaw's
    completion hook supplies stdout/stderr.  Keeping both lets the clause
    bridge prove command-lookup failures even when a pipeline's final command
    masks an earlier exit 127.
    """

    result = _stage2_result_text(raw_result)
    stderr = _stage2_named_text(raw_result, "stderr")
    return {
        "exit_code": exit_code,
        "signal": signal,
        "ok": (
            succeeded
            if succeeded is not None
            else exit_code == 0 and signal is None
        ),
        "result": _bounded_stage2_text(result),
        "stderr": _bounded_stage2_text(stderr),
    }


def _stage2_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return ""
    details = value.get("details")
    for container in (details, value):
        if not isinstance(container, Mapping):
            continue
        for key in ("aggregated", "stdout", "text"):
            text = container.get(key)
            if isinstance(text, str) and text:
                return text
    content = value.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts = [
            item.get("text")
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    nested = value.get("result")
    return _stage2_result_text(nested) if nested is not value else ""


def _stage2_named_text(value: Any, key: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    details = value.get("details")
    for container in (details, value):
        if isinstance(container, Mapping):
            text = container.get(key)
            if isinstance(text, str) and text:
                return text
    return ""


def _bounded_stage2_text(value: str, limit: int = 65_536) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _stage2_status(
    artifact: Any,
    call_telemetry: Mapping[str, Any] | None,
    kb_update_error: str | None,
) -> str:
    if call_telemetry is not None:
        quality = call_telemetry.get("telemetry_quality")
        if quality == "ok":
            return "ok" if kb_update_error is None else "collected_not_eligible"
        if quality in {"invalid", "unavailable"}:
            return str(quality)
    if isinstance(artifact, Mapping):
        calls = artifact.get("calls")
        if isinstance(calls, list) and any(
            isinstance(call, Mapping) and call.get("telemetry_quality") == "invalid"
            for call in calls
        ):
            return "invalid"
        quality = artifact.get("telemetry_quality")
        if quality == "ok":
            return "ok" if kb_update_error is None else "collected_not_eligible"
        if quality in {"invalid", "unavailable"}:
            return str(quality)
    return "unavailable" if kb_update_error else "unknown"


def _stage2_unavailable_reason(
    artifact: Any,
    call_telemetry: Mapping[str, Any] | None,
    kb_update_error: str | None,
) -> str | None:
    if call_telemetry is not None:
        reason = call_telemetry.get("unavailable_reason") or call_telemetry.get("reason")
        if isinstance(reason, str) and reason:
            return reason
        quality = call_telemetry.get("telemetry_quality")
        if quality == "invalid":
            return "call_telemetry_invalid"
        if quality == "unavailable":
            return "call_telemetry_unavailable"
    if isinstance(artifact, Mapping):
        calls = artifact.get("calls")
        if isinstance(calls, list):
            invalid = [
                call
                for call in calls
                if isinstance(call, Mapping) and call.get("telemetry_quality") == "invalid"
            ]
            if invalid:
                return "artifact_contains_invalid_call_telemetry"
        reason = artifact.get("unavailable_reason") or artifact.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    return kb_update_error


def _compact_call_telemetry(call: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if call is None:
        return None
    clauses = call.get("clauses")
    return {
        "tool_call_id": call.get("tool_call_id"),
        "command": call.get("command"),
        "telemetry_quality": call.get("telemetry_quality"),
        "formal_completeness": call.get("formal_completeness"),
        "eligible_for_kb": call.get("eligible_for_kb"),
        "clause_count": len(clauses) if isinstance(clauses, list) else 0,
        "clauses": _compact_clauses(clauses),
    }


def _compact_artifact_summary(artifact: Any) -> dict[str, Any] | None:
    if not isinstance(artifact, Mapping):
        return None
    calls = artifact.get("calls")
    return {
        "schema": artifact.get("schema"),
        "schema_version": artifact.get("version"),
        "mode": artifact.get("mode"),
        "replay_execution": artifact.get("replay_execution"),
        "collector": artifact.get("collector"),
        "container_id": artifact.get("container_id"),
        "telemetry_quality": artifact.get("telemetry_quality"),
        "formal_completeness": artifact.get("formal_completeness"),
        "telemetry_loss_total": artifact.get("telemetry_loss_total"),
        "call_count": len(calls) if isinstance(calls, list) else 0,
    }


def _compact_clauses(clauses: Any) -> list[dict[str, Any]]:
    if not isinstance(clauses, list):
        return []
    compact: list[dict[str, Any]] = []
    for row in clauses:
        if not isinstance(row, Mapping):
            continue
        compact.append(
            {
                "bin": row.get("bin"),
                "argv": row.get("argv"),
                "status": row.get("status"),
                "availability": row.get("availability"),
                "ts_start": row.get("ts_start"),
                "ts_end": row.get("ts_end"),
                "latency_ms": row.get("latency_ms"),
                "peak_cpu_cores": row.get("peak_cpu_cores"),
                "peak_memory_mb": row.get("peak_memory_mb"),
                "cumulative_cpu_s": row.get("cumulative_cpu_s"),
            }
        )
    return compact


def _clauses_from_command(command: str) -> tuple[tuple[dict[str, Any], ...], bool]:
    try:
        parsed = parse_command_clauses(command)
    except Exception:
        parsed = _fallback_parse_command_clauses(command)
    clauses = parsed.get("clauses")
    if not isinstance(clauses, list):
        return (), True
    normalized = tuple(
        clause
        for clause in (_normalize_clause(item) for item in clauses)
        if clause is not None
    )
    return normalized, bool(parsed.get("parse_failed"))


def _fallback_parse_command_clauses(command: str) -> dict[str, Any]:
    import shlex

    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return {"clauses": [], "parse_failed": True}
    if not argv:
        return {"clauses": [], "parse_failed": True}
    return {
        "clauses": [{"bin": argv[0].rsplit("/", 1)[-1], "argv": argv}],
        "parse_failed": False,
    }


def _normalize_clause(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    bin_ = value.get("bin")
    argv = value.get("argv")
    if not isinstance(bin_, str) or not bin_:
        return None
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return None
    if not argv:
        return None
    return {"bin": bin_, "argv": argv}


def _raw_params_from_start(start: dict[str, Any] | None) -> Any:
    if not isinstance(start, dict):
        return None
    input_value = start.get("input")
    if not isinstance(input_value, dict):
        return None
    return input_value.get("requested_args")


def _raw_params_from_result(raw_event: Any, raw_result: Any) -> Any:
    for candidate in (raw_event, raw_result):
        if not isinstance(candidate, dict):
            continue
        for key in ("params", "arguments", "input", "requested_args"):
            value = candidate.get(key)
            if value is not None:
                return value
    return None


def _command_for_request(request: ToolBeforeRequest) -> str | None:
    command = extract_command(request.raw_params)
    return command if command else None


def _duration_ms(record: dict[str, Any]) -> float | None:
    raw = record.get("duration_ns")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value / 1_000_000


def _span_times(record: dict[str, Any], duration_ms: float) -> tuple[float, float]:
    end = _ns_to_s(record.get("wall_time_ns"))
    if end is None:
        end = 0.0
    start = max(0.0, end - duration_ms / 1000)
    return start, end


def _ns_to_s(value: Any) -> float | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed / 1_000_000_000


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _rss_mb(value: Any) -> float | None:
    parsed = _optional_float(value)
    return None if parsed is None else parsed / (1024 * 1024)


def _cpu_ns(value: Any) -> int | None:
    parsed = _optional_float(value)
    if parsed is None or parsed < 0:
        return None
    return int(parsed * 1_000_000_000)


def _bucket_percentile_ms(
    probability_by_bucket: Sequence[float],
    buckets: LatencyBuckets,
    percentile: float,
) -> int:
    cumulative = 0.0
    for index, probability in enumerate(probability_by_bucket):
        cumulative += probability
        if cumulative >= percentile:
            return int(round(_bucket_representative_ms(index, buckets)))
    return int(round(_bucket_representative_ms(len(probability_by_bucket) - 1, buckets)))


def _bucket_representative_ms(bucket_id: int, buckets: LatencyBuckets) -> float:
    edges = buckets.edges_ms
    if bucket_id <= 0:
        return edges[0] / 2
    if bucket_id >= len(edges):
        return edges[-1] * 2
    return (edges[bucket_id - 1] + edges[bucket_id]) / 2


def _resource_class_for_bucket(bucket_id: int) -> str:
    if bucket_id <= 1:
        return "latency_short"
    if bucket_id <= 3:
        return "latency_medium"
    return "latency_long"


def _resource_class_for_duration_ms(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "unknown"
    if duration_ms <= 500:
        return "latency_short"
    if duration_ms <= 2_000:
        return "latency_medium"
    return "latency_long"


def _tool_resource_prediction_payload(
    prediction: CommandLatencyBucketPrediction,
    *,
    continuous_predictions: dict[str, Any] | None = None,
    lattice_time_predictions: Sequence[ClauseLatticeTimePredictions] = (),
) -> dict[str, Any]:
    clause_prediction = prediction.prediction
    return {
        "repo": prediction.repo,
        "command": prediction.command,
        "parse_failed": prediction.parse_failed,
        "clause_bins": list(prediction.clause_bins),
        "clause_predictions": [
            {
                "clause_index": item.clause_index,
                "bin": item.bin,
                "argv": list(item.argv),
                "prediction": _clause_bucket_prediction_payload(item.prediction),
                "unavailable_reason": item.unavailable_reason,
            }
            for item in prediction.clause_predictions
        ],
        "prediction": _clause_bucket_prediction_payload(clause_prediction),
        "unavailable_reason": prediction.unavailable_reason,
        "continuous_predictions": continuous_predictions or {},
        "lattice_time_predictions": [
            _clause_lattice_time_predictions_payload(item)
            for item in lattice_time_predictions
        ],
        "prediction_algorithms": _prediction_algorithms_payload(),
    }


def _clause_bucket_prediction_payload(
    prediction: ClauseLatencyBucketPrediction | None,
) -> dict[str, Any] | None:
    if prediction is None:
        return None
    return {
        "bucket_id": prediction.bucket_id,
        "probability_by_bucket": list(prediction.probability_by_bucket),
        "scope": prediction.scope,
        "key_kind": prediction.key_kind,
        "evidence_count": prediction.evidence_count,
        "fallback_path": list(prediction.fallback_path),
    }


def _clause_lattice_time_predictions_payload(
    outcome: ClauseLatticeTimePredictions,
) -> dict[str, Any]:
    return {
        "clause_index": outcome.clause_index,
        "bin": outcome.bin,
        "argv": list(outcome.argv),
        "predictions": [
            {
                "algorithm": prediction.algorithm,
                "prediction_ms": prediction.prediction_ms,
                "selected_features": list(prediction.selected_features),
                "evidence_count": prediction.evidence_count,
                "selected_risk": prediction.selected_risk,
                "exact_match": prediction.exact_match,
                "fallback": prediction.fallback,
                "unavailable_reason": prediction.unavailable_reason,
            }
            for prediction in outcome.predictions
        ],
    }


def _unavailable_lattice_time_predictions(
    clauses: Sequence[Mapping[str, Any]],
    *,
    reason: str,
    shell_command: bool,
) -> tuple[ClauseLatticeTimePredictions, ...]:
    outcomes: list[ClauseLatticeTimePredictions] = []
    for clause_index, clause in enumerate(clauses):
        bin_ = str(clause["bin"])
        if shell_command and not shell_bin_requires_exec_evidence(bin_):
            continue
        argv = tuple(str(value) for value in clause["argv"])
        if not bin_ or not argv:
            continue
        outcomes.append(
            ClauseLatticeTimePredictions(
                clause_index=clause_index,
                bin=bin_,
                argv=argv,
                predictions=tuple(
                    LatticeTimePrediction(
                        algorithm=algorithm,
                        prediction_ms=None,
                        selected_features=(),
                        evidence_count=0,
                        selected_risk=None,
                        exact_match=None,
                        fallback=None,
                        unavailable_reason=reason,
                    )
                    for algorithm in LATTICE_TIME_ALGORITHMS
                ),
            )
        )
    return tuple(outcomes)


def _unavailable_prediction_for_request(
    request: ToolBeforeRequest,
    *,
    repo: str,
    command: str | None,
    reason: str,
) -> CommandLatencyBucketPrediction:
    clauses, parse_failed = clauses_from_tool_request(
        request.tool_name,
        request.raw_params,
    )
    return CommandLatencyBucketPrediction(
        repo=repo,
        command=command or request.tool_name,
        parse_failed=parse_failed,
        clause_bins=tuple(str(clause["bin"]) for clause in clauses),
        prediction=None,
        unavailable_reason=reason,
        clause_predictions=tuple(
            ClauseLatencyBucketOutcome(
                clause_index=index,
                bin=str(clause["bin"]),
                argv=tuple(clause["argv"]),
                prediction=None,
                unavailable_reason=reason,
            )
            for index, clause in enumerate(clauses)
            if request.tool_name != "exec"
            or shell_bin_requires_exec_evidence(str(clause["bin"]))
        ),
    )


def _prediction_error_reason(exc: Exception) -> str:
    message = str(exc)
    if "no public global clause latency node" in message:
        return "no_clause_latency_evidence"
    return f"prediction_error:{type(exc).__name__}"


def _clause_kb_snapshot_path(artifact_dir: Path | None) -> Path | None:
    if artifact_dir is None:
        return None
    return artifact_dir / "clause-resource-kb.json"


def _runtime_kb_snapshot_path(artifact_dir: Path | None) -> Path | None:
    if artifact_dir is None:
        return None
    return artifact_dir / "runtime-tool-resource-kb.json"


def _lattice_kb_snapshot_path(artifact_dir: Path | None) -> Path | None:
    if artifact_dir is None:
        return None
    return artifact_dir / "clause-lattice-time-kb.json"


def _load_clause_kb_snapshot(
    path: Path | None,
    rejections: list[str],
) -> ClauseResourceKB | None:
    if path is None or not path.is_file():
        return None
    try:
        return ClauseResourceKB.from_json_obj(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        rejections.append(f"{path}: clause KB snapshot rejected: {exc}")
        return None


def _load_runtime_kb_snapshot(
    path: Path | None,
    rejections: list[str],
) -> RuntimeToolResourceKB | None:
    if path is None or not path.is_file():
        return None
    try:
        return RuntimeToolResourceKB.from_json_obj(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        rejections.append(f"{path}: runtime KB snapshot rejected: {exc}")
        return None


def _load_lattice_kb_snapshot(
    path: Path | None,
    rejections: list[str],
) -> LatticeTimeKB | None:
    if path is None or not path.is_file():
        return None
    try:
        return LatticeTimeKB.from_json_obj(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        rejections.append(f"{path}: lattice KB snapshot rejected: {exc}")
        return None


def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _has_public_clause_latency(kb: ClauseResourceKB) -> bool:
    public = getattr(kb, "_public", {})
    latency = public.get("latency_ms", {}) if isinstance(public, dict) else {}
    return bool(latency.get(("global", "")))


def _target_prediction_payload(prediction: TargetPrediction) -> dict[str, Any]:
    return {
        "target": prediction.target,
        "conditional_p90": prediction.conditional_p90,
        "scope": prediction.scope,
        "key_kind": prediction.key_kind,
        "evidence_count": prediction.evidence_count,
        "fallback_path": list(prediction.fallback_path),
        "note": prediction.note,
    }


def _continuous_unavailable_note(target: str, exc: Exception) -> str:
    message = str(exc)
    if message == f"no public global node for target {target!r}":
        return "no continuous evidence for target"
    return f"unavailable: {message}"


def _prediction_algorithms_payload() -> dict[str, Any]:
    return {
        "enabled": [
            {
                "name": "clause_latency_bucket",
                "family": "empirical_bucket",
                "source": "ClauseResourceKB",
                "targets": ["latency_ms"],
                "outputs": [
                    "bucket_id",
                    "probability_by_bucket",
                    "duration_p50_ms",
                    "duration_p90_ms",
                    "resource_class",
                ],
            },
            {
                "name": "lattice_shrinkage",
                "family": "context_lattice",
                "source": "LatticeTimeKB",
                "targets": ["latency_ms"],
                "outputs": ["clause_point_prediction_ms"],
            },
            {
                "name": "lattice_loso",
                "family": "context_lattice",
                "source": "LatticeTimeKB",
                "targets": ["latency_ms"],
                "outputs": ["clause_point_prediction_ms"],
            },
            {
                "name": "lattice_max_cardinality",
                "family": "context_lattice",
                "source": "LatticeTimeKB",
                "targets": ["latency_ms"],
                "outputs": ["clause_point_prediction_ms"],
            },
            {
                "name": "runtime_tool_resource_conditional_p90",
                "family": "empirical_ecdf",
                "source": "RuntimeToolResourceKB",
                "targets": ["latency_ms", "peak_cpu_cores", "peak_memory_mb"],
                "outputs": ["conditional_p90"],
            },
        ],
        "excluded": [
            {
                "name": "quantile_mlp",
                "source": "tool_resource.mlp",
                "reason": "not enabled by the sidecar; this integration uses non-MLP empirical predictors only",
            }
        ],
    }


def _expand_trace_paths(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.jsonl"))
        else:
            yield path


def _safe_artifact_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    if safe == value and len(safe) <= 128:
        return safe or "execution"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:96] or 'execution'}__{digest}"


def _execution_owners_compatible(
    expected: tuple[str | None, str | None],
    actual: tuple[str | None, str | None],
) -> bool:
    return all(
        left is None or right is None or left == right
        for left, right in zip(expected, actual, strict=True)
    )


def _execution_owner_matches_runtime(
    owner: tuple[str | None, str | None] | None,
    *,
    runtime_id: str,
    gateway_id: str | None,
) -> bool:
    if owner is None or owner[1] != runtime_id:
        return False
    return gateway_id is None or owner[0] is None or owner[0] == gateway_id
