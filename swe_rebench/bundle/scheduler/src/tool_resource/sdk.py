"""Cold-start, prediction, observation, and causal update facade."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tool_resource.runtime_kb import (
    ClauseObservation,
    ClauseResourceKB,
    CommandLatencyBucketPrediction,
    LatencyBuckets,
)


@dataclass(frozen=True)
class DockerExecutionContext:
    """Execution scope supplied by the command executor.

    The historical name remains public for compatibility. A Docker container
    id identifies sandbox work; direct host work instead supplies both a
    trusted root PID and an explicit cgroup-v2 path.
    """

    container_id: str | None
    container_executable: str
    repo: str
    artifact_path: Path
    cgroup_path: str | None = None
    trusted_root_pid: int | None = None
    source_actions: Sequence[Mapping[str, Any]] = ()

    def __post_init__(self) -> None:
        if not self.container_id and not (
            self.cgroup_path and self.trusted_root_pid is not None
        ):
            raise ValueError(
                "container_id or host cgroup_path with trusted_root_pid is required"
            )
        if not self.container_executable:
            raise ValueError("container_executable is required")
        if not self.repo:
            raise ValueError("repo is required")
        if self.trusted_root_pid is not None and self.trusted_root_pid <= 0:
            raise ValueError("trusted_root_pid must be positive")


@dataclass(frozen=True)
class CommandObservationToken:
    """Opaque collector delimiter returned before Docker execution."""

    tool_call_id: str
    command: str
    _collector_token: Any = field(repr=False)
    _start_error: str | None = field(default=None, repr=False)


class DockerCommandObserver:
    """Fail-isolated wrapper around the current Stage-2 collector."""

    def __init__(self, context: DockerExecutionContext, collector: Any) -> None:
        self.context = context
        self._collector = collector

    @classmethod
    def attach(cls, context: DockerExecutionContext) -> DockerCommandObserver:
        """Attach Stage-2 telemetry, falling back to an unavailable collector."""

        from tool_resource.telemetry import ClauseTelemetryCollector

        try:
            collector = ClauseTelemetryCollector(
                container_id=context.container_id,
                container_executable=context.container_executable,
                repo=context.repo,
                artifact_path=context.artifact_path,
                cgroup_path=context.cgroup_path,
                trusted_root_pid=context.trusted_root_pid,
                source_actions=context.source_actions,
            )
        except BaseException as exc:
            collector = ClauseTelemetryCollector.unavailable(
                container_id=context.container_id,
                repo=context.repo,
                artifact_path=context.artifact_path,
                source_actions=context.source_actions,
                reason=f"collector attach failed: {type(exc).__name__}: {exc}",
            )
        return cls(context, collector)

    @property
    def calls(self) -> list[dict[str, Any]]:
        calls = getattr(self._collector, "calls", None)
        return calls if isinstance(calls, list) else []

    @property
    def telemetry_available(self) -> bool:
        """Whether the eBPF collector is armed, not merely fail-isolated."""

        return getattr(self._collector, "state", None) == "active"

    @property
    def unavailable_reason(self) -> str | None:
        if self.telemetry_available:
            return None
        reason = getattr(self._collector, "_disabled_reason", None)
        return str(reason) if reason else "collector_unavailable"

    def start(self, tool_call_id: str, command: str) -> CommandObservationToken:
        """Start observation immediately before the Docker runner executes."""

        try:
            token = self._collector.begin_tool_call(tool_call_id, command)
            error = None
        except BaseException as exc:
            token = None
            error = self._record_error("start", exc)
        return CommandObservationToken(tool_call_id, command, token, error)

    begin_tool_call = start

    def bind_trusted_root(self, host_pid: int) -> None:
        self._collector.bind_trusted_root(host_pid)

    def finish(
        self,
        token: CommandObservationToken,
        *,
        replay_response: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Finish observation after Docker returns without raising into it."""

        if token._start_error is not None:
            return self._failure_summary(token, token._start_error)
        try:
            summary = self._collector.finish_tool_call(
                token._collector_token,
                replay_response=replay_response,
            )
            if not isinstance(summary, dict):
                raise TypeError("collector finish did not return a summary mapping")
            return summary
        except BaseException as exc:
            return self._failure_summary(token, self._record_error("finish", exc))

    finish_tool_call = finish

    def record_safety_guard_blocked(
        self,
        tool_call_id: str,
        command: str,
        replay_result: str,
    ) -> dict[str, Any]:
        try:
            summary = self._collector.record_safety_guard_blocked(
                tool_call_id,
                command,
                replay_result,
            )
            if not isinstance(summary, dict):
                raise TypeError("collector guard result is not a summary mapping")
            return summary
        except BaseException as exc:
            token = CommandObservationToken(tool_call_id, command, None)
            return self._failure_summary(
                token,
                self._record_error("safety_guard", exc),
            )

    def add_integrity_error(self, message: str) -> None:
        try:
            self._collector.add_integrity_error(message)
        except BaseException:
            pass

    def finalize(self, *, replay_execution: str = "completed") -> str | None:
        try:
            self._collector.finalize(replay_execution=replay_execution)
            return None
        except BaseException as exc:
            return self._record_error("finalize", exc)

    def _record_error(self, phase: str, exc: BaseException) -> str:
        message = f"telemetry {phase} failed: {type(exc).__name__}: {exc}"
        self.add_integrity_error(message)
        return message

    def _failure_summary(
        self,
        token: CommandObservationToken,
        message: str,
    ) -> dict[str, Any]:
        summary = {
            "version": 2,
            "tool_call_id": token.tool_call_id,
            "tool_trace_ref": token.tool_call_id,
            "command": token.command,
            "telemetry_quality": "unavailable",
            "eligible_for_kb": False,
            "invalid_reasons": [
                {"kind": "observer_failure", "detail": message}
            ],
            "clauses": [],
            "integrity": {"status": "failed", "errors": [message]},
        }
        try:
            if not self.calls or self.calls[-1] != summary:
                self.calls.append(summary)
        except BaseException:
            pass
        return summary


@dataclass(frozen=True)
class CommandRun:
    """Prediction and observer state returned before Docker execution."""

    tool_call_id: str
    command: str
    ts_start: float
    prediction: CommandLatencyBucketPrediction | None
    prediction_error: str | None
    _owner: object = field(repr=False)
    _run_id: int = field(repr=False)
    _observer: DockerCommandObserver = field(repr=False)
    _observation_token: CommandObservationToken = field(repr=False)


@dataclass(frozen=True)
class CommandResult:
    """Workload result plus finalized telemetry and KB update status."""

    run: CommandRun
    workload_result: Mapping[str, Any] | None
    call_telemetry: Mapping[str, Any]
    telemetry_artifact: Mapping[str, Any] | None
    kb_observations_added: int
    kb_update_error: str | None


@dataclass(frozen=True)
class ColdStartReport:
    """Accepted and rejected Stage-2 inputs used to initialize the SDK."""

    artifacts_seen: int
    artifacts_accepted: int
    calls_seen: int
    eligible_calls_loaded: int
    calls_withheld: int
    observations_loaded: int
    rejections: tuple[str, ...]

    @property
    def artifacts_rejected(self) -> int:
        return len(self.rejections)


class ToolResourceSDK:
    """Cold-start knowledge plus one complete Docker command transaction."""

    def __init__(
        self,
        kb: ClauseResourceKB,
        latency_buckets: LatencyBuckets,
        cold_start_report: ColdStartReport | None = None,
    ) -> None:
        self._kb = kb
        self.latency_buckets = latency_buckets
        self.cold_start_report = cold_start_report
        self._owner = object()
        self._next_run_id = 0
        self._pending_run_ids: set[int] = set()

    @classmethod
    def from_traces(
        cls,
        trace_paths: str | Path | Iterable[str | Path],
        latency_buckets: LatencyBuckets,
    ) -> ToolResourceSDK:
        """Fit frozen public knowledge from valid Stage-2 telemetry artifacts."""

        paths = (
            [Path(trace_paths)]
            if isinstance(trace_paths, (str, Path))
            else [Path(path) for path in trace_paths]
        )
        if not paths:
            raise ValueError("at least one cold-start trace is required")
        observations: list[ClauseObservation] = []
        rejections: list[str] = []
        accepted = 0
        calls_seen = 0
        eligible_calls_loaded = 0
        for path in paths:
            try:
                artifact = _load_valid_artifact(path)
                repo = str(
                    artifact.get("provenance", {}).get("repo") or "public"
                )
                artifact_observations: list[ClauseObservation] = []
                artifact_eligible_calls = 0
                for call in artifact["calls"]:
                    if call.get("eligible_for_kb") is not True:
                        continue
                    artifact_eligible_calls += 1
                    artifact_observations.extend(
                        _observations_from_call(
                            repo,
                            call,
                            require_timestamps=False,
                        )
                    )
            except (TypeError, ValueError) as exc:
                detail = str(exc)
                rejections.append(
                    detail if detail.startswith(str(path)) else f"{path}: {detail}"
                )
                continue
            observations.extend(artifact_observations)
            calls_seen += len(artifact["calls"])
            eligible_calls_loaded += artifact_eligible_calls
            accepted += 1
        if accepted == 0:
            detail = rejections[0] if rejections else "no inputs"
            raise ValueError(f"no valid cold-start telemetry artifacts: {detail}")
        return cls(
            ClauseResourceKB.fit_public(observations),
            latency_buckets,
            ColdStartReport(
                artifacts_seen=len(paths),
                artifacts_accepted=accepted,
                calls_seen=calls_seen,
                eligible_calls_loaded=eligible_calls_loaded,
                calls_withheld=calls_seen - eligible_calls_loaded,
                observations_loaded=len(observations),
                rejections=tuple(rejections),
            ),
        )

    def start_command(
        self,
        context: DockerExecutionContext,
        tool_call_id: str,
        command: str,
        *,
        ts_start: float | None = None,
    ) -> CommandRun:
        """Parse, query, predict, and start telemetry before Docker execution."""

        if context.artifact_path.exists():
            raise ValueError(
                f"command telemetry artifact already exists: {context.artifact_path}"
            )
        query_ts = time.time() if ts_start is None else float(ts_start)
        if not math.isfinite(query_ts):
            raise ValueError("ts_start must be finite")
        try:
            prediction = self._kb.predict_command_latency_bucket(
                context.repo,
                command,
                query_ts,
                self.latency_buckets,
            )
            prediction_error = None
        except Exception as exc:
            prediction = None
            prediction_error = f"{type(exc).__name__}: {exc}"
        observer = DockerCommandObserver.attach(context)
        token = observer.start(tool_call_id, command)
        run_id = self._next_run_id
        self._next_run_id += 1
        self._pending_run_ids.add(run_id)
        return CommandRun(
            tool_call_id=tool_call_id,
            command=command,
            ts_start=query_ts,
            prediction=prediction,
            prediction_error=prediction_error,
            _owner=self._owner,
            _run_id=run_id,
            _observer=observer,
            _observation_token=token,
        )

    def finish_command(
        self,
        run: CommandRun,
        workload_result: Mapping[str, Any] | None,
        *,
        replay_execution: str = "completed",
    ) -> CommandResult:
        """Finalize telemetry, then update the KB only from a valid artifact."""

        if run._owner is not self._owner:
            raise ValueError("command run belongs to a different SDK")
        if run._run_id not in self._pending_run_ids:
            raise ValueError("command run has already been finished")
        self._pending_run_ids.remove(run._run_id)
        call_telemetry = run._observer.finish(
            run._observation_token,
            replay_response=workload_result,
        )
        finalize_error = run._observer.finalize(replay_execution=replay_execution)
        artifact: dict[str, Any] | None = None
        try:
            if finalize_error is not None:
                raise ValueError(finalize_error)
            artifact = _read_artifact(run._observer.context.artifact_path)
            _validate_artifact(
                run._observer.context.artifact_path,
                artifact,
                expected_container_id=run._observer.context.container_id,
                expected_repo=run._observer.context.repo,
            )
            calls = [
                call
                for call in artifact["calls"]
                if call.get("tool_call_id") == run.tool_call_id
            ]
            if len(calls) != 1:
                raise ValueError(
                    f"final artifact has {len(calls)} calls for {run.tool_call_id!r}"
                )
            call = calls[0]
            if call.get("command") != run.command:
                raise ValueError("final artifact command does not match the request")
            if call.get("eligible_for_kb") is not True:
                raise ValueError("final command telemetry is not eligible for KB")
            observations = _observations_from_call(
                run._observer.context.repo,
                call,
                require_timestamps=True,
            )
            for observation in observations:
                self._kb.observe_completed_clause(observation)
            update_error = None
        except Exception as exc:
            observations = []
            update_error = f"{type(exc).__name__}: {exc}"
        return CommandResult(
            run=run,
            workload_result=workload_result,
            call_telemetry=call_telemetry,
            telemetry_artifact=artifact,
            kb_observations_added=len(observations),
            kb_update_error=update_error,
        )


def _load_valid_artifact(path: Path) -> dict[str, Any]:
    artifact = _read_artifact(path)
    _validate_artifact(path, artifact)
    return artifact


def _read_artifact(path: Path) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read telemetry artifact {path}: {exc}") from exc
    if not isinstance(artifact, dict) or artifact.get("version") != 2:
        raise ValueError(f"{path}: expected Stage-2 telemetry artifact version 2")
    return artifact


def _validate_artifact(
    path: Path,
    artifact: Mapping[str, Any],
    *,
    expected_container_id: str | None = None,
    expected_repo: str | None = None,
) -> None:
    if artifact.get("mode") != "clause":
        raise ValueError(f"{path}: expected clause telemetry mode")
    if artifact.get("replay_execution") not in {"completed", "failed"}:
        raise ValueError(f"{path}: replay execution is incomplete")
    if artifact.get("cleanup") != "ok":
        raise ValueError(f"{path}: collector cleanup is not ok")
    calls = artifact.get("calls")
    if not isinstance(calls, list) or not all(
        isinstance(call, Mapping) for call in calls
    ):
        raise ValueError(f"{path}: calls must be a list of mappings")
    status_model = artifact.get("status_model")
    if status_model == "call_granular_v1":
        if artifact.get("telemetry_quality") != "ok":
            raise ValueError(
                f"{path}: artifact telemetry quality is "
                f"{artifact.get('telemetry_quality')!r}; not eligible for KB"
            )
        if artifact.get("collection_validity") != "valid":
            raise ValueError(f"{path}: collection is not valid")
        if artifact.get("formal_completeness") not in {"complete", "partial"}:
            raise ValueError(f"{path}: formal completeness is unavailable")
        integrity = artifact.get("integrity")
        if not isinstance(integrity, Mapping) or integrity.get("status") != "ok":
            raise ValueError(f"{path}: collector integrity is not ok")
    elif status_model is not None:
        raise ValueError(f"{path}: unsupported status model {status_model!r}")
    elif not _legacy_collector_healthy(artifact):
        raise ValueError(f"{path}: legacy collector health is not usable")
    if (
        expected_container_id is not None
        and artifact.get("container_id") != expected_container_id
    ):
        raise ValueError(f"{path}: container identity does not match the request")
    provenance = artifact.get("provenance")
    if (
        expected_repo is not None
        and (
            not isinstance(provenance, Mapping)
            or provenance.get("repo") != expected_repo
        )
    ):
        raise ValueError(f"{path}: repository identity does not match the request")


def _legacy_collector_healthy(artifact: Mapping[str, Any]) -> bool:
    collector = artifact.get("collector")
    loss = artifact.get("telemetry_loss_total")
    return (
        artifact.get("telemetry_quality") in {"ok", "invalid"}
        and artifact.get("cleanup") == "ok"
        and isinstance(collector, Mapping)
        and collector.get("state_before_close") == "active"
        and collector.get("unavailable_call_count") == 0
        and isinstance(loss, Mapping)
        and loss.get("total") == 0
    )


def _observations_from_call(
    repo: str,
    call: Mapping[str, Any],
    *,
    require_timestamps: bool,
) -> list[ClauseObservation]:
    rows = call.get("clauses")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("eligible telemetry has invalid clauses")
    observations: list[ClauseObservation] = []
    for row in rows:
        availability = row.get("availability")
        if not isinstance(availability, Mapping):
            raise ValueError("eligible clause has invalid availability")
        if availability.get("latency") == "ok":
            observations.append(
                _observation_from_clause(
                    repo,
                    row,
                    require_timestamps=require_timestamps,
                )
            )
    return observations


def _observation_from_clause(
    repo: str,
    row: Mapping[str, Any],
    *,
    require_timestamps: bool,
) -> ClauseObservation:
    latency_ms = _required_nonnegative_float(row.get("latency_ms"), "latency_ms")
    raw_start = row.get("ts_start")
    raw_end = row.get("ts_end")
    if raw_start is None and raw_end is None and not require_timestamps:
        ts_start = 0.0
        ts_end = latency_ms / 1000.0
    else:
        ts_start = _required_finite_float(raw_start, "ts_start")
        ts_end = _required_finite_float(raw_end, "ts_end")
    argv = row.get("argv")
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("eligible clause has invalid argv")
    bin_ = row.get("bin")
    if not isinstance(bin_, str) or not bin_:
        raise ValueError("eligible clause has invalid bin")
    return ClauseObservation(
        repo=repo,
        bin=bin_,
        argv=tuple(argv),
        ts_start=ts_start,
        ts_end=ts_end,
        latency_ms=latency_ms,
        peak_cpu_cores=_optional_finite_float(row.get("peak_cpu_cores")),
        sampled_peak_rss_mb=_optional_finite_float(
            row.get("sampled_peak_rss_mb")
        ),
        cpu_ns_cumulative=_optional_nonnegative_int(row.get("cpu_ns_cumulative")),
        in_loop=bool(row.get("in_loop", False)),
        in_pipe=bool(row.get("in_pipe", False)),
        in_subst=bool(row.get("in_subst", False)),
        pipeline_position=int(row.get("pipeline_position", -1)),
    )


def _required_finite_float(value: Any, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError(f"eligible clause has invalid {name}")
    return float(value)


def _required_nonnegative_float(value: Any, name: str) -> float:
    result = _required_finite_float(value, name)
    if result < 0.0:
        raise ValueError(f"eligible clause has negative {name}")
    return result


def _optional_finite_float(value: Any) -> float | None:
    return None if value is None else _required_finite_float(value, "measurement")


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("eligible clause has invalid cumulative CPU")
    return value


__all__ = [
    "CommandObservationToken",
    "CommandResult",
    "CommandRun",
    "DockerCommandObserver",
    "DockerExecutionContext",
    "ToolResourceSDK",
]
