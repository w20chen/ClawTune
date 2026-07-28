from __future__ import annotations

import json
import math
import os
import shlex
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_scheduler.contracts.models import ToolBeforeRequest, ToolCompletedEvent, ToolPrediction
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from agent_scheduler.tool_resource_commands import extract_command
from tool_resource.features import parse_command_clauses
from tool_resource.metrics import ecdf_quantile
from tool_resource.runtime_kb import (
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
        self._sdk = ToolResourceSDK(kb, buckets)
        self._runs_by_execution_id: dict[str, CommandRun] = {}
        self._telemetry_by_execution_id: dict[str, ExecutionTelemetrySummary] = {}
        self._starts: dict[str, ToolBeforeRequest] = {}

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
                rejections=tuple(rejections),
            ),
            repo=repo,
            artifact_dir=artifact_dir,
            container_executable=container_executable,
            clause_kb_snapshot_path=snapshot_path,
            runtime_kb_snapshot_path=runtime_snapshot_path,
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
        query_ts = time.time()
        try:
            clauses, parse_failed = clauses_from_tool_request(
                request.tool_name,
                request.raw_params,
            )
            prediction = self.kb.predict_command_latency_bucket_from_clauses(
                self.repo,
                clauses,
                query_ts,
                self.buckets,
                command=command or request.tool_name,
                parse_failed=parse_failed,
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
                        repo=self.repo,
                        command=command,
                        reason=_prediction_error_reason(exc),
                    ),
                    continuous_predictions=continuous_predictions,
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
            ),
        )

    def record_tool_started(self, request: ToolBeforeRequest) -> None:
        self._starts[_tool_key(request.tool_call_id, request.event_id)] = request

    def observe_completion(
        self,
        event: ToolCompletedEvent,
        sample: ToolRuntimeSample,
    ) -> int:
        start = self._starts.pop(_tool_key(event.tool_call_id, event.event_id), None)
        if start is None and event.tool_call_id is not None:
            start = self._pop_start_by_tool_call_id(event.tool_call_id)
        completed_call = completed_call_from_completion(
            event,
            sample,
            repo=self.repo,
            start=start,
        )
        if completed_call is not None:
            self.continuous_kb.observe_completed_call(completed_call)
            self._persist_runtime_kb()
        return 1 if completed_call is not None else 0

    def begin_execution(
        self,
        *,
        execution_id: str,
        tool_call_id: str | None,
        command: str,
        container_id: str | None,
        repo: str | None = None,
        cgroup_path: str | None = None,
    ) -> bool:
        if execution_id in self._runs_by_execution_id:
            existing = self._runs_by_execution_id[execution_id]
            observer = getattr(existing, "_observer", None)
            return bool(getattr(observer, "telemetry_available", True))
        previous = self._telemetry_by_execution_id.get(execution_id)
        if previous is not None and previous.started:
            return previous.status != "unavailable"
        if self.artifact_dir is None or not container_id:
            reason = (
                "artifact_dir_unconfigured"
                if self.artifact_dir is None
                else "container_id_unavailable"
            )
            self._telemetry_by_execution_id[execution_id] = ExecutionTelemetrySummary(
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                artifact_path=None,
                started=False,
                status="unavailable",
                unavailable_reason=reason,
            )
            return False
        artifact_path = self.artifact_dir / f"{_safe_artifact_name(execution_id)}.json"
        context = DockerExecutionContext(
            container_id=container_id,
            container_executable=self.container_executable,
            repo=repo or self.repo,
            artifact_path=artifact_path,
            cgroup_path=cgroup_path,
        )
        try:
            run = self._sdk.start_command(context, tool_call_id or execution_id, command)
        except Exception as exc:
            self._telemetry_by_execution_id[execution_id] = ExecutionTelemetrySummary(
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                artifact_path=str(artifact_path),
                started=False,
                status="unavailable",
                unavailable_reason=f"start_failed:{type(exc).__name__}: {exc}",
            )
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
        self._runs_by_execution_id[execution_id] = run
        self._telemetry_by_execution_id[execution_id] = ExecutionTelemetrySummary(
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            artifact_path=str(artifact_path),
            started=True,
            status="started" if telemetry_available else "unavailable",
            unavailable_reason=unavailable_reason,
        )
        return telemetry_available

    def finish_execution(
        self,
        *,
        execution_id: str,
        exit_code: int | None,
        signal: int | None,
    ) -> ExecutionTelemetrySummary:
        run = self._runs_by_execution_id.pop(execution_id, None)
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
        replay_execution = "completed" if exit_code == 0 and signal is None else "failed"
        workload_result = {"exit_code": exit_code, "signal": signal}
        try:
            result = self._sdk.finish_command(
                run,
                workload_result,
                replay_execution=replay_execution,
            )
        except Exception as exc:
            summary = ExecutionTelemetrySummary(
                execution_id=execution_id,
                tool_call_id=run.tool_call_id,
                artifact_path=str(run._observer.context.artifact_path),
                started=True,
                status="unavailable",
                unavailable_reason=f"finish_failed:{type(exc).__name__}: {exc}",
            )
            self._telemetry_by_execution_id[execution_id] = summary
            return summary
        if result.kb_observations_added:
            self._persist_clause_kb()
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
            status=_stage2_status(result.telemetry_artifact, call_telemetry, result.kb_update_error),
            unavailable_reason=_stage2_unavailable_reason(
                result.telemetry_artifact,
                call_telemetry,
                result.kb_update_error,
            ),
            kb_observations_added=result.kb_observations_added,
            kb_update_error=result.kb_update_error,
            call_telemetry=_compact_call_telemetry(call_telemetry),
            artifact_summary=_compact_artifact_summary(result.telemetry_artifact),
        )
        self._telemetry_by_execution_id[execution_id] = summary
        return summary

    def execution_telemetry(self, execution_id: str) -> ExecutionTelemetrySummary | None:
        return self._telemetry_by_execution_id.get(execution_id)

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

    def _continuous_predictions_for_request(
        self,
        request: ToolBeforeRequest,
        command: str | None,
        ts_start: float,
        ambient_before_mb: float | None = None,
    ) -> dict[str, Any]:
        query = ToolCallQuery(
            repo=self.repo,
            tool_name=request.tool_name,
            command=command,
            ts_start=ts_start,
            ambient_before_mb=ambient_before_mb,
        )
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
        query = ToolCallQuery(
            repo=self.repo,
            tool_name=request.tool_name,
            command=command,
            ts_start=ts_start,
        )
        try:
            self.continuous_kb._advance(ts_start)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                self.continuous_kb.query(query)
            except Exception:
                pass
        except Exception:
            pass
        try:
            values, _scope, _kind, _path = self.continuous_kb._select(  # type: ignore[attr-defined]
                self.repo,
                "latency_ms",
                request.tool_name,
                command,
            )
        except Exception:
            return None, None
        if not values:
            return None, None
        return (
            max(0, int(round(ecdf_quantile(values, 0.5)))),
            max(0, int(round(ecdf_quantile(values, 0.9)))),
        )

    def _pop_start_by_tool_call_id(self, tool_call_id: str) -> ToolBeforeRequest | None:
        for key, request in list(self._starts.items()):
            if request.tool_call_id == tool_call_id:
                self._starts.pop(key, None)
                return request
        return None


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
            completed_call = _completed_call_from_tool_span(start, record, repo=repo)
            if completed_call is not None:
                completed_calls.append(completed_call)
    return _LoadedTrace(tool_spans_seen, tuple(observations), tuple(completed_calls))


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
    return ClauseObservation(
        repo=repo,
        bin=str(clause["bin"]),
        argv=tuple(str(item) for item in clause["argv"]),
        ts_start=ts_start,
        ts_end=ts_end,
        latency_ms=max(0.0, float(event.duration_ms)),
        peak_cpu_cores=sample.cpu_utilization_avg_cores,
        sampled_peak_rss_mb=_rss_mb(sample.rss_bytes_peak),
        cpu_ns_cumulative=_cpu_ns(sample.cpu_time_delta_s),
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
    return CompletedCall(
        repo=repo,
        tool_name=event.tool_name,
        command=command,
        ts_start=ts_start,
        ts_end=ts_end,
        censored=False,
        peak_cpu_cores=sample.cpu_utilization_avg_cores,
        peak_cpu_cores_eligible=sample.cpu_utilization_avg_cores is not None,
        peak_memory_mb=peak_memory_mb,
        peak_memory_mb_eligible=peak_memory_mb is not None and ambient_before_mb is not None,
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
    return ClauseObservation(
        repo=repo,
        bin=str(clause["bin"]),
        argv=tuple(str(item) for item in clause["argv"]),
        ts_start=ts_start,
        ts_end=ts_end,
        latency_ms=duration_ms,
        peak_cpu_cores=_optional_float(resources.get("cpu_utilization_avg_cores")),
        sampled_peak_rss_mb=_rss_mb(resources.get("rss_peak_bytes")),
        cpu_ns_cumulative=_cpu_ns(resources.get("cpu_time_s")),
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
    return CompletedCall(
        repo=repo,
        tool_name=tool_name,
        command=command,
        ts_start=ts_start,
        ts_end=ts_end,
        censored=False,
        peak_cpu_cores=peak_cpu_cores,
        peak_cpu_cores_eligible=peak_cpu_cores is not None,
        peak_memory_mb=peak_memory_mb,
        peak_memory_mb_eligible=peak_memory_mb is not None and ambient_before_mb is not None,
        ambient_before_mb=ambient_before_mb,
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
) -> dict[str, Any]:
    clause_prediction = prediction.prediction
    return {
        "repo": prediction.repo,
        "command": prediction.command,
        "parse_failed": prediction.parse_failed,
        "clause_bins": list(prediction.clause_bins),
        "prediction": (
            None
            if clause_prediction is None
            else {
                "bucket_id": clause_prediction.bucket_id,
                "probability_by_bucket": list(clause_prediction.probability_by_bucket),
                "scope": clause_prediction.scope,
                "key_kind": clause_prediction.key_kind,
                "evidence_count": clause_prediction.evidence_count,
                "fallback_path": list(clause_prediction.fallback_path),
            }
        ),
        "unavailable_reason": prediction.unavailable_reason,
        "continuous_predictions": continuous_predictions or {},
        "prediction_algorithms": _prediction_algorithms_payload(),
    }


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
    return safe[:128] or "execution"


def _tool_key(tool_call_id: str | None, event_id: str) -> str:
    return tool_call_id or event_id
