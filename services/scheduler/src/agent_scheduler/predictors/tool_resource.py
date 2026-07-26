from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_scheduler.contracts.models import ToolBeforeRequest, ToolCompletedEvent, ToolPrediction
from agent_scheduler.predictors.exec_classifier import extract_command
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from tool_resource.features import parse_command_clauses
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, LatencyBuckets
from tool_resource.sdk import CommandRun, DockerExecutionContext, ToolResourceSDK


@dataclass(frozen=True)
class ToolResourceLoadReport:
    stage2_traces_seen: int
    stage2_traces_loaded: int
    openclaw_traces_seen: int
    openclaw_traces_accepted: int
    openclaw_tool_spans_seen: int
    observations_loaded: int
    kb_available: bool
    rejections: tuple[str, ...]


@dataclass(frozen=True)
class OpenClawTraceLoadReport:
    traces_seen: int
    traces_accepted: int
    tool_spans_seen: int
    observations_loaded: int
    rejections: tuple[str, ...]


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
    ) -> None:
        self.kb = kb
        self.buckets = buckets
        self.report = report
        self.repo = repo
        self.artifact_dir = artifact_dir
        self.container_executable = container_executable
        self._sdk = ToolResourceSDK(kb, buckets)
        self._runs_by_execution_id: dict[str, CommandRun] = {}
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
            observations.extend(loaded.observations)

        kb: ClauseResourceKB
        kb_has_public_evidence = False
        if observations:
            try:
                kb = ClauseResourceKB.fit_public(observations)
                kb_has_public_evidence = True
            except ValueError as exc:
                rejections.append(f"fit_public: {exc}")
                kb = ClauseResourceKB()
        else:
            kb = ClauseResourceKB()

        return cls(
            kb=kb,
            buckets=buckets,
            report=ToolResourceLoadReport(
                stage2_traces_seen=len(stage2_paths),
                stage2_traces_loaded=stage2_loaded,
                openclaw_traces_seen=len(openclaw_paths),
                openclaw_traces_accepted=openclaw_accepted,
                openclaw_tool_spans_seen=openclaw_spans_seen,
                observations_loaded=len(observations),
                kb_available=kb_has_public_evidence,
                rejections=tuple(rejections),
            ),
            repo=repo,
            artifact_dir=artifact_dir,
            container_executable=container_executable,
        )

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

    async def predict(self, request: ToolBeforeRequest) -> ToolPrediction:
        clauses, parse_failed = clauses_from_tool_request(request.tool_name, request.raw_params)
        if not clauses:
            return ToolPrediction(resource_class="unknown")
        try:
            command = _command_for_request(request)
            prediction = self.kb.predict_command_latency_bucket_from_clauses(
                self.repo,
                clauses,
                time.time(),
                self.buckets,
                command=command or request.tool_name,
                parse_failed=parse_failed,
            )
        except Exception:
            return ToolPrediction(resource_class="unknown")
        if prediction.prediction is None:
            return ToolPrediction(resource_class="unknown")

        bucket_prediction = prediction.prediction
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
            duration_p50_ms=duration_p50_ms,
            duration_p90_ms=duration_p90_ms,
            resource_class=_resource_class_for_bucket(bucket_prediction.bucket_id),
            confidence=max(bucket_prediction.probability_by_bucket, default=0.0),
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
        observation = observation_from_completion(event, sample, repo=self.repo, start=start)
        if observation is None:
            return 0
        self.kb.observe_completed_clause(observation)
        return 1

    def begin_execution(
        self,
        *,
        execution_id: str,
        tool_call_id: str | None,
        command: str,
        container_id: str | None,
        repo: str | None = None,
    ) -> bool:
        if execution_id in self._runs_by_execution_id:
            return False
        if self.artifact_dir is None or not container_id:
            return False
        artifact_path = self.artifact_dir / f"{_safe_artifact_name(execution_id)}.json"
        context = DockerExecutionContext(
            container_id=container_id,
            container_executable=self.container_executable,
            repo=repo or self.repo,
            artifact_path=artifact_path,
        )
        try:
            run = self._sdk.start_command(context, tool_call_id or execution_id, command)
        except Exception:
            return False
        self._runs_by_execution_id[execution_id] = run
        return True

    def finish_execution(
        self,
        *,
        execution_id: str,
        exit_code: int | None,
        signal: int | None,
    ) -> int:
        run = self._runs_by_execution_id.pop(execution_id, None)
        if run is None:
            return 0
        replay_execution = "completed" if exit_code == 0 and signal is None else "failed"
        workload_result = {"exit_code": exit_code, "signal": signal}
        try:
            result = self._sdk.finish_command(
                run,
                workload_result,
                replay_execution=replay_execution,
            )
        except Exception:
            return 0
        return result.kb_observations_added

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


def load_openclaw_trace_observations(path: Path, *, repo: str) -> _LoadedTrace:
    if not path.is_file():
        raise ValueError("trace path is not a file")
    starts: dict[str, dict[str, Any]] = {}
    observations: list[ClauseObservation] = []
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
            observation = _observation_from_tool_span(start, record, repo=repo)
            if observation is not None:
                observations.append(observation)
    return _LoadedTrace(tool_spans_seen, tuple(observations))


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


def _read_stage2_artifact(path: Path) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Stage-2 artifact: {exc}") from exc
    if not isinstance(artifact, dict) or artifact.get("version") != 2:
        raise ValueError("expected Stage-2 telemetry artifact version 2")
    if artifact.get("mode") != "clause":
        raise ValueError("expected clause telemetry mode")
    calls = artifact.get("calls")
    if not isinstance(calls, list):
        raise ValueError("artifact calls must be a list")
    return artifact


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
        clauses = call.get("clauses")
        if not isinstance(clauses, list):
            continue
        for clause in clauses:
            observation = _stage2_clause_observation(repo, clause)
            if observation is not None:
                observations.append(observation)
    return observations


def _stage2_clause_observation(repo: str, row: Any) -> ClauseObservation | None:
    if not isinstance(row, dict):
        return None
    availability = row.get("availability")
    if isinstance(availability, dict) and availability.get("latency") != "ok":
        return None
    latency_ms = _optional_float(row.get("latency_ms"))
    bin_ = row.get("bin")
    argv = row.get("argv")
    if latency_ms is None or latency_ms < 0 or not isinstance(bin_, str) or not bin_:
        return None
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return None
    ts_start = _optional_float(row.get("ts_start"))
    ts_end = _optional_float(row.get("ts_end"))
    if ts_start is None or ts_end is None or ts_end < ts_start:
        ts_start = 0.0
        ts_end = latency_ms / 1000
    return ClauseObservation(
        repo=repo,
        bin=bin_,
        argv=tuple(argv),
        ts_start=ts_start,
        ts_end=ts_end,
        latency_ms=latency_ms,
        peak_cpu_cores=_optional_float(row.get("peak_cpu_cores")),
        sampled_peak_rss_mb=_optional_float(row.get("sampled_peak_rss_mb")),
        cpu_ns_cumulative=_optional_int(row.get("cpu_ns_cumulative")),
        in_loop=bool(row.get("in_loop", False)),
        in_pipe=bool(row.get("in_pipe", False)),
        in_subst=bool(row.get("in_subst", False)),
        pipeline_position=_optional_int(row.get("pipeline_position")) or -1,
    )


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


def _optional_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if value >= 0 else None


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
