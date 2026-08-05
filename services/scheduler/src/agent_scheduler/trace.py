from __future__ import annotations

import hashlib
import json
import queue
import re
import time
import threading
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_scheduler.contracts.models import (
    ModelEvent,
    ToolBeforeRequest,
    ToolCompletedEvent,
    ToolPrediction,
)
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from agent_scheduler.identity import (
    correlation_key,
    owner_key,
    owner_prefix_matches,
    owners_compatible,
)


def _safe_filename(segment: str | None) -> str:
    if not segment:
        return "unknown"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", segment)[:64]


class _FlushMarker:
    """Queue sentinel that makes the writer thread signal a flush event.

    Because the write queue is strictly FIFO, the writer thread only reaches
    this marker after every record enqueued before it has been written to
    disk.  ``AgentTestBenchTraceWriter.flush`` uses it to make the
    ``{"stored": True}`` acknowledgement durable.
    """

    __slots__ = ("event",)

    def __init__(self, event: threading.Event) -> None:
        self.event = event


class AgentTestBenchTraceWriter:
    """Per-run trace writer. Creates one JSONL file per run under trace_dir.

    Files are named: {agent_id}_{session_id}_{run_id}.jsonl
    """

    def __init__(
        self,
        trace_dir: Path,
        *,
        scaffold: str = "openclaw",
        max_messages_bytes: int = 131_072,
        default_repo: str = "openclaw",
    ) -> None:
        self.trace_dir = trace_dir.resolve()
        self.scaffold = scaffold
        self._max_messages_bytes = max_messages_bytes
        self._default_repo = default_repo
        self._instance_id = str(uuid4())
        self._lock = threading.Lock()
        self._model_starts: dict[tuple[str | None, ...], ModelEvent] = {}
        self._tool_starts: dict[tuple[str | None, ...], ToolBeforeRequest] = {}
        self._tool_predictions: dict[tuple[str | None, ...], dict[str, Any]] = {}
        self._tool_resource_telemetry: dict[str, dict[str, Any]] = {}
        self._recent_proxy_calls: list[dict[str, Any]] = []
        self._proxy_activity: dict[str, int] = {}
        self._seq_counters: dict[tuple[str | None, ...], int] = {}
        self._files: dict[tuple[str | None, ...], Path] = {}
        self._metadata_written: set[str] = set()  # track files that already have metadata
        # Maps tool_call_id → parent LLM span_id for resolving parent_span_id
        # on tool spans. Populated when an LLM span produces tool calls.
        self._tool_parent_map: dict[tuple[str | None, ...], str] = {}
        self._write_queue: queue.Queue[object] = queue.Queue()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="clawtune-trace-writer",
            daemon=True,
        )
        self._writer_thread.start()
        self.trace_dir.mkdir(parents=True, exist_ok=True)

    def _file_for_run(
        self,
        gateway_id: str | None,
        runtime_id: str | None,
        run_id: str | None,
        session_id: str | None,
        agent_id: str | None,
    ) -> Path | None:
        """Return the trace file for a run.

        Keys writers by run_id (primary) or session_id (fallback).
        Uses instance_id only as a last-resort key to prevent data loss,
        but logs a warning since it can cause cross-run accumulation.

        Returns None when no identifiable key is available at all.
        """
        run_key = run_id or session_id
        if not run_key:
            # Last resort: instance_id. Log a warning so operators
            # can detect when the plugin isn't sending run_id/session_id.
            import logging
            _log = logging.getLogger(__name__)
            _log.warning(
                "trace: no run_id or session_id, falling back to instance_id "
                "(may cause cross-run accumulation). run_id=%s session_id=%s agent_id=%s",
                run_id, session_id, agent_id,
            )
            run_key = self._instance_id
        runtime_key = runtime_id or "legacy"
        key = (gateway_id, runtime_id, agent_id, session_id, run_key)
        if key in self._files:
            return self._files[key]
        session = _safe_filename(session_id)
        run = _safe_filename(run_id)
        # Note: agent_id is included per-record in the JSONL content.
        # It is omitted from the filename because model hooks do not
        # expose agent_id — an OpenClaw limitation.
        identity_digest = _identity_digest(key)
        filename = (
            f"{_safe_filename(runtime_key)}__{session}_{run}"
            f"__{identity_digest}.jsonl"
        )
        filepath = self.trace_dir / filename
        self._files[key] = filepath
        return filepath

    def _next_seq(self, event: ToolBeforeRequest | ToolCompletedEvent | ModelEvent) -> int:
        key = (*owner_key(event), event.run_id or self._instance_id)
        current = self._seq_counters.get(key, 0)
        current += 1
        self._seq_counters[key] = current
        return current

    def record_tool_started(self, event: ToolBeforeRequest) -> None:
        self._tool_starts[correlation_key(event)] = event

    def record_tool_prediction(
        self,
        event: ToolBeforeRequest,
        prediction: ToolPrediction,
    ) -> None:
        self._tool_predictions[correlation_key(event)] = (
            prediction.model_dump(mode="json")
        )

    def record_tool_resource_telemetry(
        self,
        execution_id: str | None,
        telemetry: Any,
    ) -> None:
        if execution_id is None or telemetry is None:
            return
        if hasattr(telemetry, "model_dump"):
            payload = telemetry.model_dump(mode="json")
        elif hasattr(telemetry, "__dataclass_fields__"):
            payload = {
                key: getattr(telemetry, key)
                for key in telemetry.__dataclass_fields__
            }
        elif isinstance(telemetry, dict):
            payload = dict(telemetry)
        else:
            return
        self._tool_resource_telemetry[execution_id] = payload

    def record_tool(self, event: ToolCompletedEvent, sample: ToolRuntimeSample) -> None:
        start_key, start = self._pop_tool_start(event)
        prediction = self._pop_tool_prediction(event, start_key)
        tool_resource_telemetry = self._pop_tool_resource_telemetry(event.execution_id)
        tool_args = None if start is None else start.raw_params
        ts_start, ts_end = _tool_timestamps(sample, event.duration_ms)
        self._record_tool_v6(
            event,
            sample,
            start,
            prediction,
            tool_resource_telemetry,
            tool_args,
            ts_start,
            ts_end,
        )

    def _record_tool_v6(
        self,
        event: ToolCompletedEvent,
        sample: ToolRuntimeSample,
        start: ToolBeforeRequest | None,
        prediction: dict[str, Any] | None,
        tool_resource_telemetry: dict[str, Any] | None,
        tool_args: Any,
        ts_start: float,
        ts_end: float,
    ) -> None:
        trace_id = event.run_id or self._instance_id
        span_id = event.tool_call_id or event.event_id
        # Resolve parent LLM span: look up by tool_call_id, then by event_id.
        parent_span_id = self._tool_parent_map.get(
            correlation_key(event)
        )
        run_id = event.run_id
        session_id = event.session_id
        agent_id = event.agent_id or _agent_id_from_session_key(event.session_key)

        seq_no = self._next_seq(event)

        wall_start_ns = str(int(ts_start * 1_000_000_000))
        wall_end_ns = str(int(ts_end * 1_000_000_000))
        # Use monotonic clock for durations so they are immune to wall-clock
        # adjustments (NTP, leap seconds).  Wall-clock is preserved separately.
        duration_ns_value = int(max(0, event.duration_ms) * 1_000_000)
        mono_end_ns_value = time.monotonic_ns()
        mono_start_ns = str(max(0, mono_end_ns_value - duration_ns_value))
        mono_end_ns = str(mono_end_ns_value)
        duration_ns = str(duration_ns_value)

        tool_exit_code = _tool_exit_code(event.raw_result, event.tool_name)
        status_code = _tool_status_code(event, tool_exit_code)
        status_message = _tool_status_message(event, tool_exit_code)

        # Prefer the completed-event scope (populated by the plugin from the
        # execution registry after the tool finishes) over the before-event
        # scope (always null since the tool hasn't started yet).
        scope = _first_present(
            event.resource_scope,
            start.resource_scope if start is not None else None,
        )
        has_pid = scope is not None and scope.pid is not None
        has_resource_scope = has_pid or (scope is not None and scope.cgroup_path is not None)
        shared_runtime = _is_shared_runtime_scope(scope)
        shared_sandbox = _is_shared_sandbox_scope(scope)

        filepath = self._file_for_run(
            event.gateway_id,
            event.runtime_id,
            run_id,
            session_id,
            agent_id,
        )
        self._ensure_metadata(filepath)

        # span_start
        self._append(filepath, {
            "schema_version": 6,
            "record_type": "span_start",
            "gateway_id": event.gateway_id,
            "runtime_id": event.runtime_id,
            "repo": event.repo or self._default_repo,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "session_id": session_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "sequence_no": seq_no,
            "kind": "tool",
            "name": event.tool_name,
            "wall_time_ns": wall_start_ns,
            "monotonic_time_ns": mono_start_ns,
            "input": {"requested_args": tool_args},
            "prediction": prediction,
            "execution": {
                "mode": "launcher" if event.execution_id else "in_process_or_runtime_managed",
                "execution_id": event.execution_id,
            },
        })

        # Compute monitor coverage fields from the runtime sample.
        _mon_start_wall, _mon_end_wall, _mon_start_mono, _mon_end_mono = (
            _monitor_timestamps_ns(sample)
        )
        _action_dur_ns = int(duration_ns)
        _cov_dur_ns, _cov_ratio, _cov_reason = _coverage(
            action_start_wall_ns=int(wall_start_ns),
            action_end_wall_ns=int(wall_end_ns),
            action_duration_ns=_action_dur_ns,
            monitor_start_wall_ns=_mon_start_wall,
            monitor_end_wall_ns=_mon_end_wall,
            has_pid=has_resource_scope,
            internal_tool_no_process=event.execution_id is None and not has_resource_scope,
            shared_runtime_process=shared_runtime,
            shared_sandbox_container=shared_sandbox,
        )

        # span_end
        self._append(filepath, {
            "schema_version": 6,
            "record_type": "span_end",
            "gateway_id": event.gateway_id,
            "runtime_id": event.runtime_id,
            "repo": event.repo or self._default_repo,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "session_id": session_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "sequence_no": seq_no,
            "kind": "tool",
            "name": event.tool_name,
            "wall_time_ns": wall_end_ns,
            "monotonic_time_ns": mono_end_ns,
            "duration_ns": duration_ns,
            "status": {"code": status_code, "message": status_message},
            "output": {"exit_code": _trace_exit_code(event.tool_name, status_code, tool_exit_code), "result": event.raw_result},
            "execution": {
                "mode": "launcher" if event.execution_id else "in_process_or_runtime_managed",
                "execution_id": event.execution_id,
                "payload_pid": scope.pid if scope is not None else None,
                "payload_pid_start_time_ticks": scope.root_starttime_ticks if scope is not None else None,
                "cgroup_path": scope.cgroup_path if scope is not None else None,
                "pid_role": "payload_root" if has_pid else None,
                "source": scope.source if scope is not None else None,
                "tool_resource": tool_resource_telemetry,
            },
            "resources": {
                "attribution_status": _v6_attribution(sample, scope),
                "attribution_source": scope.attribution_source if scope is not None else None,
                "scope": "cgroup" if (scope is not None and scope.cgroup_path) else ("process_tree" if has_pid else "none"),
                "quality": _v6_quality(sample.sampling_quality, _cov_reason),
                "monitor_start_wall_time_ns": str(_mon_start_wall) if _mon_start_wall is not None else None,
                "monitor_end_wall_time_ns": str(_mon_end_wall) if _mon_end_wall is not None else None,
                "monitor_start_monotonic_ns": str(_mon_start_mono) if _mon_start_mono is not None else None,
                "monitor_end_monotonic_ns": str(_mon_end_mono) if _mon_end_mono is not None else None,
                "monitor_duration_ns": str(sample.monitor_duration_ms * 1_000_000),
                "coverage_duration_ns": str(_cov_dur_ns) if _cov_dur_ns is not None else None,
                "action_duration_ns": duration_ns,
                "coverage_ratio": _cov_ratio,
                "coverage_reason": _cov_reason,
                "cpu_time_s": sample.cpu_time_delta_s,
                "cgroup_cpu_time_s": sample.cpu_time_delta_s if sample.monitor_source == "cgroup-v2" else None,
                "rss_peak_bytes": sample.rss_bytes_peak,
                "memory_rss_bytes_before": sample.rss_bytes_before,
                "memory_rss_bytes_after": sample.rss_bytes_after,
                "disk_read_bytes_delta": sample.read_bytes_delta,
                "disk_write_bytes_delta": sample.write_bytes_delta,
                "net_rx_bytes_delta": sample.net_rx_bytes_delta,
                "net_tx_bytes_delta": sample.net_tx_bytes_delta,
                "ctx_switches_delta": sample.ctx_switches_delta,
                "cpu_utilization_avg_cores": sample.cpu_utilization_avg_cores,
                "cpu_utilization_avg_pct": sample.cpu_utilization_avg_pct,
                "disk_read_bytes_per_s": sample.disk_read_bytes_per_s,
                "disk_write_bytes_per_s": sample.disk_write_bytes_per_s,
                "net_rx_bytes_per_s": sample.net_rx_bytes_per_s,
                "net_tx_bytes_per_s": sample.net_tx_bytes_per_s,
                "sampling_interval_ms": sample.sampling_interval_ms,
                "sampling_point_count": sample.sampling_point_count,
                "sampling_quality": sample.sampling_quality,
                "resource_timeline": sample.resource_timeline,
                "resource_timeline_truncated": sample.resource_timeline_truncated,
                "resource_class": sample.resource_class,
                "target_pid": sample.target_pid,
                "process_count_before": sample.process_count_before,
                "process_count_after": sample.process_count_after,
                "monitor_source": sample.monitor_source,
            },
        })

    def record_model(self, event: ModelEvent) -> None:
        key = correlation_key(event, event.call_id)
        if event.event_type == "model_call_started":
            self._model_starts[key] = event
            return
        start = self._model_starts.pop(key, None)
        proxy_call = self._pop_recent_proxy_call(event)
        ts_end = _parse_timestamp(event.occurred_at)
        duration_s = (event.duration_ms or 0) / 1000
        ts_start = _parse_timestamp(start.occurred_at) if start is not None else ts_end - duration_s
        proxy_data = proxy_call.get("data", {}) if isinstance(proxy_call, dict) else {}
        self._record_model_v6(event, start, ts_start, ts_end, proxy_data)

    def _record_model_v6(
        self,
        event: ModelEvent,
        start: ModelEvent | None,
        ts_start: float,
        ts_end: float,
        proxy_data: dict[str, Any],
    ) -> None:
        trace_id = event.run_id or self._instance_id
        span_id = event.call_id or event.event_id
        run_id = event.run_id
        session_id = event.session_id
        agent_id = event.agent_id or _agent_id_from_session_key(event.session_key)

        seq_no = self._next_seq(event)

        wall_start_ns = str(int(ts_start * 1_000_000_000))
        wall_end_ns = str(int(ts_end * 1_000_000_000))
        # Use monotonic clock for durations so they are immune to wall-clock
        # adjustments (NTP, leap seconds). Wall-clock is preserved separately.
        # Derive the monotonic start from end minus duration so the span
        # invariant (mono_end - mono_start == duration_ns) holds, matching
        # _record_tool_v6.
        duration_ns_value = int(max(0, event.duration_ms or 0) * 1_000_000)
        mono_end_ns_value = time.monotonic_ns()
        mono_start_ns = str(max(0, mono_end_ns_value - duration_ns_value))
        mono_end_ns = str(mono_end_ns_value)
        duration_ns = str(duration_ns_value)

        status_code = "ok" if event.outcome in ("completed", "ok", "success") else ("error" if event.outcome == "error" else "unknown")

        raw_messages = _first_present(
            None if start is None else start.raw_input,
            proxy_data.get("messages_in"),
        )
        messages = _truncate_messages(raw_messages, self._max_messages_bytes)

        filepath = self._file_for_run(
            event.gateway_id,
            event.runtime_id,
            run_id,
            session_id,
            agent_id,
        )
        self._ensure_metadata(filepath)

        self._append(filepath, {
            "schema_version": 6,
            "record_type": "span_start",
            "gateway_id": event.gateway_id,
            "runtime_id": event.runtime_id,
            "repo": event.repo or self._default_repo,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": None,
            "session_id": session_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "sequence_no": seq_no,
            "kind": "llm",
            "name": event.model or "unknown-model",
            "wall_time_ns": wall_start_ns,
            "monotonic_time_ns": mono_start_ns,
            "input": {"requested_args": None, "messages": messages},
            "execution": {"mode": None, "execution_id": None},
        })

        output_content = _llm_output_content(event.raw_output, proxy_data)

        self._append(filepath, {
            "schema_version": 6,
            "record_type": "span_end",
            "gateway_id": event.gateway_id,
            "runtime_id": event.runtime_id,
            "repo": event.repo or self._default_repo,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": None,
            "session_id": session_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "sequence_no": seq_no,
            "kind": "llm",
            "name": event.model or "unknown-model",
            "wall_time_ns": wall_end_ns,
            "monotonic_time_ns": mono_end_ns,
            "duration_ns": duration_ns,
            "status": {"code": status_code, "message": None},
            "output": {"content": output_content},
            "execution": {"mode": None, "execution_id": None},
            "resources": {
                "attribution_status": "not_applicable",
                "scope": "none",
                "quality": "unknown",
                "monitor_start_wall_time_ns": None,
                "monitor_end_wall_time_ns": None,
                "monitor_start_monotonic_ns": None,
                "monitor_end_monotonic_ns": None,
                "coverage_duration_ns": None,
                "action_duration_ns": duration_ns,
                "coverage_ratio": None,
                "coverage_reason": "not_applicable",
            },
        })

        # Register tool_call_id → parent span mapping so child tool spans
        # can resolve their parent_span_id.  Handles both direct OpenAI-style
        # tool_calls and content-wrapped proxy formats.
        for candidate in (output_content, event.raw_output, proxy_data.get("content"), proxy_data.get("raw_response")):
            for tc_id in _extract_tool_call_ids(candidate):
                self._tool_parent_map[
                    correlation_key(event, tc_id)
                ] = span_id

    def record_llm_proxy_call(
        self,
        *,
        runtime_id: str | None,
        action_id: str | None,
        provider: str | None,
        model: str | None,
        messages_in: Any | None,
        content: Any | None,
        raw_request: Any | None,
        raw_response: Any | None,
        ts_start: float,
        ts_end: float,
        status_code: int,
        stream: bool,
        error: str | None = None,
    ) -> None:
        duration_ms = max(0.0, (ts_end - ts_start) * 1000)
        record = {
            "type": "action",
            "action_type": "llm_call",
            "action_id": action_id or f"llm-proxy-{uuid4()}",
            "run_id": None,
            "session_id": None,
            "session_key": None,
            "agent_id": None,
            "runtime_id": runtime_id,
            "ts_start": ts_start,
            "ts_end": ts_end,
            "data": {
                "provider": provider,
                "model": model,
                "messages_in": messages_in,
                "content": content,
                "duration_ms": int(duration_ms),
                "llm_latency_ms": duration_ms,
                "outcome": "error" if error else "completed",
                "context_token_budget": None,
                "proxy": {"status_code": status_code, "stream": stream, "error": error},
                "openclaw_started_event": None,
                "openclaw_ended_event": None,
                "raw_request": raw_request,
                "raw_response": raw_response,
            },
        }
        self._remember_proxy_call(record)

    def begin_proxy_activity(self, runtime_id: str | None) -> None:
        if runtime_id is None:
            return
        with self._lock:
            self._proxy_activity[runtime_id] = (
                self._proxy_activity.get(runtime_id, 0) + 1
            )

    def end_proxy_activity(self, runtime_id: str | None) -> None:
        if runtime_id is None:
            return
        with self._lock:
            remaining = self._proxy_activity.get(runtime_id, 0) - 1
            if remaining > 0:
                self._proxy_activity[runtime_id] = remaining
            else:
                self._proxy_activity.pop(runtime_id, None)

    def active_runtime_operations(self, runtime_id: str) -> int:
        with self._lock:
            return self._proxy_activity.get(runtime_id, 0)

    def _ensure_metadata(self, filepath: Path) -> None:
        """Write metadata once per file. Never truncates existing data."""
        key = str(filepath)
        if key in self._metadata_written:
            return
        # Write first, then mark as written — so a failed write can be retried.
        self._append(filepath, self._metadata_record())
        self._metadata_written.add(key)

    def _append(self, filepath: Path, record: dict[str, Any]) -> None:
        """Enqueue a serialised record for asynchronous disk write.

        JSON serialisation runs on the calling thread; the blocking disk
        write is performed by the dedicated trace-writer thread so that
        the FastAPI event loop is never blocked on filesystem I/O.
        """
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self._write_queue.put((filepath, line))

    def _writer_loop(self) -> None:
        """Dedicated thread that drains the write queue to disk."""
        while True:
            item = self._write_queue.get()
            if item is None:  # graceful shutdown sentinel
                break
            if isinstance(item, _FlushMarker):
                item.event.set()
                continue
            filepath, line = item
            try:
                filepath.parent.mkdir(parents=True, exist_ok=True)
                with filepath.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                # Trace persistence is best-effort; a lost span_end is
                # preferable to a crashed sidecar.
                pass

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until every record enqueued so far is written to disk.

        The write queue is FIFO, so enqueueing a flush marker behind the
        pending records and waiting for the writer thread to reach it makes
        the earlier records durable.  Returns ``False`` if the writer thread
        could not drain within *timeout* seconds.
        """
        event = threading.Event()
        self._write_queue.put(_FlushMarker(event))
        return event.wait(timeout)

    def close(self) -> None:
        """Drain pending writes and stop the writer thread."""
        self._write_queue.put(None)
        self._writer_thread.join(timeout=10.0)
        if self._writer_thread.is_alive():
            # Thread may be stuck on a slow / hung filesystem; it is a
            # daemon thread so the process can still exit.
            pass

    def _metadata_record(self) -> dict[str, Any]:
        return {
            "schema_version": 6,
            "record_type": "trace_metadata",
            "trace_format_version": 6,
            "scaffold": self.scaffold,
            "mode": "collect",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def _pop_tool_start(
        self,
        event: ToolCompletedEvent,
    ) -> tuple[tuple[str | None, ...] | None, ToolBeforeRequest | None]:
        event_key = correlation_key(event)
        start = self._tool_starts.pop(event_key, None)
        if start is not None or event.tool_call_id is not None:
            return event_key, start
        matches = [
            (key, value)
            for key, value in self._tool_starts.items()
            if value.tool_name == event.tool_name
            and value.runtime_id == event.runtime_id
            and owners_compatible(value, event)
        ]
        if len(matches) != 1:
            return None, None
        key, value = matches[0]
        self._tool_starts.pop(key, None)
        return key, value

    def _pop_tool_prediction(
        self,
        event: ToolCompletedEvent,
        start_key: tuple[str | None, ...] | None,
    ) -> dict[str, Any] | None:
        event_key = correlation_key(event)
        prediction = self._tool_predictions.pop(event_key, None)
        if prediction is not None:
            return prediction
        if start_key is not None and start_key != event_key:
            return self._tool_predictions.pop(start_key, None)
        return None

    def _pop_tool_resource_telemetry(
        self,
        execution_id: str | None,
    ) -> dict[str, Any] | None:
        if execution_id is None:
            return None
        return self._tool_resource_telemetry.pop(execution_id, None)

    def _remember_proxy_call(self, record: dict[str, Any]) -> None:
        self._recent_proxy_calls.append(record)
        if len(self._recent_proxy_calls) > 2_048:
            del self._recent_proxy_calls[:-2_048]

    def release_runtime(
        self,
        runtime_id: str,
        gateway_id: str | None = None,
    ) -> None:
        """Release in-memory routing state after a runtime has drained."""

        runtime_files = {
            str(path)
            for key, path in self._files.items()
            if owner_prefix_matches(key, runtime_id)
            and (gateway_id is None or key[0] == gateway_id)
        }
        for mapping in (
            self._model_starts,
            self._tool_starts,
            self._tool_predictions,
            self._seq_counters,
            self._files,
            self._tool_parent_map,
        ):
            for key in [
                key
                for key in mapping
                if owner_prefix_matches(key, runtime_id)
                and (gateway_id is None or key[0] == gateway_id)
            ]:
                mapping.pop(key, None)
        self._metadata_written.difference_update(runtime_files)
        self._recent_proxy_calls = [
            record
            for record in self._recent_proxy_calls
            if record.get("runtime_id") != runtime_id
        ]
        self._proxy_activity.pop(runtime_id, None)

    def _pop_recent_proxy_call(self, event: ModelEvent) -> dict[str, Any] | None:
        event_ts = _parse_timestamp(event.occurred_at)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for index, record in enumerate(self._recent_proxy_calls):
            if record.get("runtime_id") != event.runtime_id:
                continue
            # Tolerant gateway check, matching the convention used by
            # `belongs_to_runtime` (identity.py) and `executions.py`: a
            # mismatch is only a hard rejection when BOTH sides carry a
            # non-null gateway_id and they differ.  Proxy captures recorded
            # by the LLM proxy never carry a gateway_id (the proxy only sees
            # the runtime credential), so enforcing an equality here would
            # silently discard every proxy capture whenever the model event
            # has a gateway_id set (swe-rebench always sets "swe-rebench").
            record_gateway_id = record.get("gateway_id")
            if (
                event.gateway_id is not None
                and record_gateway_id is not None
                and record_gateway_id != event.gateway_id
            ):
                continue
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            if event.model is not None and data.get("model") != event.model:
                continue
            ts_end = record.get("ts_end")
            try:
                delta = abs(event_ts - float(ts_end))
            except (TypeError, ValueError):
                continue
            if delta <= 10:
                candidates.append((index, record))
        if not candidates:
            return None
        if len(candidates) > 1:
            event_tool_calls = set(_extract_tool_call_ids(event.raw_output))
            if event_tool_calls:
                correlated = [
                    (index, record)
                    for index, record in candidates
                    if event_tool_calls.intersection(
                        _extract_tool_call_ids(
                            record.get("data", {}).get("raw_response")
                            if isinstance(record.get("data"), dict)
                            else None
                        )
                    )
                ]
                if len(correlated) == 1:
                    candidates = correlated
        # Time-nearest is not a safe identity boundary for concurrent
        # sessions.  If exact response correlation cannot disambiguate, keep
        # the hook data as-is instead of attaching another session's payload.
        if len(candidates) != 1:
            return None
        index, record = candidates[0]
        self._recent_proxy_calls.pop(index)
        return record


def _parse_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return datetime.now(timezone.utc).timestamp()


def _tool_exit_code(raw_result: Any | None, tool_name: str) -> int | None:
    if tool_name != "exec":
        return None
    if isinstance(raw_result, int) and raw_result >= 0:
        return raw_result
    if not isinstance(raw_result, dict):
        return None
    direct = _first_int(raw_result, ("exit_code", "exitCode"))
    if direct is not None:
        return direct
    details = raw_result.get("details")
    if isinstance(details, dict):
        return _first_int(details, ("exit_code", "exitCode"))
    return None


def _tool_status_code(event: ToolCompletedEvent, exit_code: int | None) -> str:
    details = event.raw_result.get("details") if isinstance(event.raw_result, dict) else None
    raw_status = details.get("status") if isinstance(details, dict) else None
    raw_error = _raw_tool_error(event.raw_result)
    if event.error_type == "timeout":
        return "timeout"
    if event.error_type == "cancelled":
        return "cancelled"
    if exit_code is not None and exit_code != 0:
        return "error"
    if raw_status in {"error", "failed"} or raw_error is not None:
        return "error"
    if event.succeeded:
        return "ok"
    if event.error_type:
        return "error"
    return "unknown"


def _tool_status_message(event: ToolCompletedEvent, exit_code: int | None) -> str | None:
    if event.error_type:
        return event.error_type
    raw_error = _raw_tool_error(event.raw_result)
    if raw_error is not None:
        return raw_error
    if exit_code is not None and exit_code != 0:
        return f"exit_code_{exit_code}"
    return None


def _trace_exit_code(tool_name: str, status_code: str, exit_code: int | None) -> int | None:
    if exit_code is not None:
        return exit_code
    if tool_name == "exec" and status_code == "ok":
        return 0
    return None


def _raw_tool_error(raw_result: Any | None) -> str | None:
    if not isinstance(raw_result, dict):
        return None
    direct = raw_result.get("error")
    if isinstance(direct, str) and direct:
        return direct
    details = raw_result.get("details")
    if isinstance(details, dict):
        detail_error = details.get("error") or details.get("failureKind")
        if isinstance(detail_error, str) and detail_error:
            return detail_error
    return None


def _first_int(value: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, int) and item >= 0:
            return item
    return None


def _tool_timestamps(sample: ToolRuntimeSample, duration_ms: int) -> tuple[float, float]:
    ts_end = sample.ended_at
    duration_s = max(0.0, duration_ms / 1000)
    if duration_s > 0:
        # Monitoring begins while the scheduler decision is still in flight,
        # so its window may be substantially longer than the duration reported
        # by OpenClaw for the actual tool action.  The action window must use
        # the reported duration and keep the monitor window separate.
        return ts_end - duration_s, ts_end
    ts_start = sample.started_at
    if ts_end < ts_start:
        ts_end = ts_start
    return ts_start, ts_end


def _identity_digest(parts: tuple[str | None, ...]) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:12]


def _agent_id_from_session_key(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) >= 2 and parts[0] == "agent" and parts[1]:
        return parts[1]
    return None


def _first_present(*values: Any) -> Any | None:
    for value in values:
        # Skip None and empty strings — the proxy captures "" for
        # tool-call-only LLM turns where the API returns content:"".
        if value is not None and value != "":
            return value
    return None


def _llm_output_content(raw_output: Any | None, proxy_data: dict[str, Any]) -> Any | None:
    proxy_content = proxy_data.get("content")
    raw_response = proxy_data.get("raw_response")
    tool_calls: list[Any] = []
    for candidate in (proxy_content, raw_response, raw_output):
        tool_calls = _extract_tool_calls(candidate)
        if tool_calls:
            break
    if not tool_calls:
        return _first_present(raw_output, proxy_content, raw_response)

    text_content = _first_present(
        _extract_text_content(raw_output),
        _extract_text_content(proxy_content),
        _extract_text_content(raw_response),
    )
    return {
        "content": text_content or "",
        "tool_calls": tool_calls,
    }


def _extract_text_content(value: Any | None) -> Any | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return value
    content = value.get("content")
    if content not in (None, ""):
        return content
    choices = value.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or choices[0].get("delta")
        if isinstance(message, dict) and message.get("content") not in (None, ""):
            return message.get("content")
    return None


def _extract_tool_calls(output_content: Any) -> list[Any]:
    if isinstance(output_content, dict):
        tool_calls = output_content.get("tool_calls")
        if isinstance(tool_calls, list):
            return tool_calls
        for choice in _list_value(output_content.get("choices")):
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message") or choice.get("delta")
            if isinstance(msg, dict) and isinstance(msg.get("tool_calls"), list):
                return msg["tool_calls"]
    return []


def _extract_tool_call_ids(output_content: Any) -> list[str]:
    """Extract tool_call IDs from an LLM output content value.

    Handles multiple shapes produced by the proxy and OpenClaw hooks:
      - {"tool_calls": [{"id": "..."}, ...]}   (direct tool-calls dict)
      - [{"id": "..."}, ...]                    (bare tool-calls list)
      - {"choices": [{"message": {"tool_calls": [...]}}]} (raw API response)
    """
    ids: list[str] = []
    if not isinstance(output_content, dict):
        return ids
    ids.extend(_collect_ids(_extract_tool_calls(output_content)))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            unique.append(tid)
    return unique


def _collect_ids(items: list[Any]) -> list[str]:
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])
    return ids


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _monitor_timestamps_ns(sample: ToolRuntimeSample) -> tuple[int | None, int | None, int | None, int | None]:
    """Extract monitor start/end timestamps (wall + monotonic) in nanoseconds.

    Returns (monitor_start_wall_ns, monitor_end_wall_ns,
             monitor_start_mono_ns, monitor_end_mono_ns).
    """
    msw = int(sample.monitor_start_wall_s * 1_000_000_000) if sample.monitor_start_wall_s > 0 else None
    mew = int(sample.monitor_end_wall_s * 1_000_000_000) if sample.monitor_end_wall_s > 0 else None
    msm = int(sample.monitor_start_monotonic_s * 1_000_000_000) if sample.monitor_start_monotonic_s else None
    mem = int(sample.monitor_end_monotonic_s * 1_000_000_000) if sample.monitor_end_monotonic_s else None
    return msw, mew, msm, mem


def _coverage(
    *,
    action_start_wall_ns: int,
    action_end_wall_ns: int,
    action_duration_ns: int,
    monitor_start_wall_ns: int | None,
    monitor_end_wall_ns: int | None,
    has_pid: bool,
    internal_tool_no_process: bool = False,
    shared_runtime_process: bool = False,
    shared_sandbox_container: bool = False,
) -> tuple[int | None, float | None, str]:
    """Compute coverage duration, ratio, and reason.

    Follows the trace schema v6 formula:
      coverage_duration_ns = max(0, min(action_end, monitor_end) - max(action_start, monitor_start))
      coverage_ratio = coverage_duration_ns / action_duration_ns
    """
    if not has_pid:
        return None, None, "internal_tool_no_process" if internal_tool_no_process else "pid_unavailable"
    if monitor_start_wall_ns is None or monitor_end_wall_ns is None:
        return None, None, "clock_data_missing"

    overlap_ns = max(
        0,
        min(action_end_wall_ns, monitor_end_wall_ns)
        - max(action_start_wall_ns, monitor_start_wall_ns),
    )

    if action_duration_ns <= 0:
        return overlap_ns, None, "full_window" if overlap_ns > 0 else "monitor_window_no_overlap"

    # Keep the public trace invariant even when upstream clocks are rounded or
    # a malformed sample claims a monitor interval wider than its action.
    overlap_ns = min(overlap_ns, action_duration_ns)
    ratio = max(0.0, min(1.0, overlap_ns / action_duration_ns))

    if shared_sandbox_container:
        reason = "shared_sandbox_container"
    elif shared_runtime_process and ratio > 0.0:
        reason = "shared_runtime_process"
    elif ratio >= 0.99:
        reason = "full_window"
    elif ratio <= 0.0:
        reason = "monitor_window_no_overlap"
    else:
        reason = "pid_registered_late"

    return overlap_ns, ratio, reason


def _truncate_messages(messages: Any, max_bytes: int) -> Any:
    """Truncate message content so the serialized form stays within max_bytes.

    Operates on a COPY — never mutates the original.  When the limit is
    exceeded the first message is kept intact and subsequent messages are
    dropped; if even the first message exceeds the limit its content is
    truncated with a marker.
    """
    if messages is None:
        return None
    if max_bytes <= 0:
        return None

    # Fast path: serialise and check
    try:
        line = json.dumps(messages, separators=(",", ":"))
    except (TypeError, ValueError):
        return messages  # can't serialise, return as-is

    if len(line.encode("utf-8")) <= max_bytes:
        return messages

    # Need to truncate.  Work on a copy.
    if not isinstance(messages, list):
        return _truncate_single_message(messages, max_bytes)

    kept: list[Any] = []
    for msg in messages:
        candidate = json.dumps(kept + [msg], separators=(",", ":"))
        if len(candidate.encode("utf-8")) <= max_bytes:
            kept.append(msg)
        else:
            break

    if not kept and messages:
        # Even the first message is too large — truncate its content.
        first = dict(messages[0]) if isinstance(messages[0], dict) else messages[0]
        if isinstance(first, dict) and "content" in first:
            first["content"] = _truncate_string(
                str(first["content"]), max_bytes - 200
            ) + "\n\n[TRUNCATED — message exceeds trace limit]"
        kept = [first]

    return kept if kept else None


def _truncate_single_message(msg: Any, max_bytes: int) -> Any:
    """Truncate a single message-like object."""
    if isinstance(msg, dict) and "content" in msg:
        overhead = len(
            json.dumps({k: "" for k in msg}, separators=(",", ":")).encode("utf-8")
        )
        limit = max(0, max_bytes - overhead - 100)
        return {**msg, "content": _truncate_string(str(msg["content"]), limit) + "\n\n[TRUNCATED]"}
    return str(msg)[:max_bytes]


def _truncate_string(value: str, max_bytes: int) -> str:
    """Truncate a string to at most max_bytes when encoded as UTF-8."""
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    # Walk back from the cut point to avoid splitting a multi-byte character.
    truncated = encoded[:max_bytes]
    return truncated.decode("utf-8", errors="ignore")


def _is_shared_runtime_scope(scope: Any | None) -> bool:
    if scope is None:
        return False
    return (
        getattr(scope, "source", None) == "openclaw-runtime"
        or getattr(scope, "attribution_source", None) == "shared-runtime-process"
    )


def _is_shared_sandbox_scope(scope: Any | None) -> bool:
    if scope is None:
        return False
    return (
        getattr(scope, "source", None) == "openclaw-sandbox"
        or getattr(scope, "attribution_source", None) == "shared-sandbox-container"
    )


def _v6_attribution(sample: ToolRuntimeSample, scope: Any | None = None) -> str:
    """Map legacy attribution_status to v6 AttributionStatus."""
    if _is_shared_runtime_scope(scope) or _is_shared_sandbox_scope(scope):
        return "partially_attributed"
    mapping = {
        "pid": "attributed",
        "cgroup-v2": "attributed",
        "unattributed": "unattributed",
        "pid-unavailable": "failed",
    }
    return mapping.get(sample.attribution_status, "unknown")


def _v6_quality(sampling_quality: str, coverage_reason: str | None) -> str:
    if sampling_quality in {"unknown", "unattributed", "unavailable"}:
        return "unknown"
    if coverage_reason == "full_window":
        return "complete"
    return "partial"
