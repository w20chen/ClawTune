from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from agent_scheduler.contracts.models import ResourceScope, ToolBeforeRequest, ToolCompletedEvent
from agent_scheduler.monitoring.process import ProcessResourceSampler, ResourceSnapshot
from agent_scheduler.predictors.exec_classifier import extract_operation


@dataclass(frozen=True)
class ToolRuntimeSample:
    event_id: str
    tool_call_id: str | None
    tool_name: str
    operation: str | None
    started_at: float
    ended_at: float
    duration_ms: int
    monitor_duration_ms: int
    monitor_start_wall_s: float
    monitor_end_wall_s: float
    monitor_start_monotonic_s: float | None
    monitor_end_monotonic_s: float | None
    cpu_time_delta_s: float | None
    rss_bytes_before: int | None
    rss_bytes_after: int | None
    read_bytes_delta: int | None
    write_bytes_delta: int | None
    net_rx_bytes_delta: int | None
    net_tx_bytes_delta: int | None
    ctx_switches_delta: int | None
    rss_bytes_peak: int | None
    cpu_utilization_avg_cores: float | None
    cpu_utilization_avg_pct: float | None
    disk_read_bytes_per_s: float | None
    disk_write_bytes_per_s: float | None
    net_rx_bytes_per_s: float | None
    net_tx_bytes_per_s: float | None
    sampling_interval_ms: int
    sampling_point_count: int
    sampling_quality: str
    resource_timeline: list[dict[str, Any]]
    resource_timeline_truncated: bool
    resource_class: str
    target_pid: int | None
    process_count_before: int | None
    process_count_after: int | None
    attribution_status: str
    monitor_source: str


@dataclass(frozen=True)
class _ActiveTool:
    request: ToolBeforeRequest
    snapshot: ResourceSnapshot
    latest_snapshot: ResourceSnapshot
    rss_bytes_peak: int | None
    timeline: list[dict[str, Any]]
    snapshot_count: int
    timeline_truncated: bool
    resource_class: str
    operation: str | None


class RealtimeToolMonitor:
    def __init__(
        self,
        sampler: ProcessResourceSampler | None = None,
        max_active: int = 10_000,
        poll_interval_s: float = 0.05,
        max_timeline_points: int = 2_000,
    ) -> None:
        self.sampler = sampler or ProcessResourceSampler()
        self.max_active = max_active
        self.poll_interval_s = poll_interval_s
        self.max_timeline_points = max_timeline_points
        self._active: dict[str, _ActiveTool] = {}
        self._lock = threading.RLock()
        self._poller = threading.Thread(target=self._poll_active, daemon=True)
        self._poller.start()

    def begin(self, request: ToolBeforeRequest, resource_class: str) -> None:
        key = self._key(request.tool_call_id, request.event_id)
        snapshot = self.sampler.snapshot(request.resource_scope)
        with self._lock:
            if len(self._active) >= self.max_active:
                oldest = next(iter(self._active))
                self._active.pop(oldest, None)
            self._active[key] = _ActiveTool(
                request=request,
                snapshot=snapshot,
                latest_snapshot=snapshot,
                rss_bytes_peak=snapshot.rss_bytes,
                timeline=[_timeline_point(snapshot)],
                snapshot_count=1,
                timeline_truncated=False,
                resource_class=resource_class,
                operation=extract_operation(request),
            )

    def complete(self, completion: ToolCompletedEvent) -> ToolRuntimeSample:
        key = self._key(completion.tool_call_id, completion.event_id)
        with self._lock:
            active = self._active.pop(key, None)
            if active is None and completion.tool_call_id is not None:
                active = self._pop_by_tool_call_id(completion.tool_call_id)
            if active is None and completion.tool_call_id is None:
                active = self._pop_unique_by_tool_name(completion.tool_name)
        completion_scope = completion.resource_scope
        if completion_scope is None and active is not None:
            completion_scope = active.request.resource_scope
        end = self.sampler.snapshot(completion_scope)
        final_snapshot_available = end.available
        used_latest_snapshot = False
        if active is not None and not end.available and active.latest_snapshot.available:
            end = active.latest_snapshot
            used_latest_snapshot = True
        if active is None:
            start = end
            operation = None
            resource_class = "unknown"
            rss_bytes_peak = end.rss_bytes
            timeline = [_timeline_point(end)]
            snapshot_count = 1
            timeline_truncated = False
        else:
            start = active.snapshot
            operation = active.operation
            resource_class = active.resource_class
            rss_bytes_peak = active.rss_bytes_peak
            timeline = list(active.timeline)
            snapshot_count = active.snapshot_count
            timeline_truncated = active.timeline_truncated
            if final_snapshot_available and end.captured_at != active.latest_snapshot.captured_at:
                timeline, timeline_truncated = _append_timeline(
                    timeline,
                    _timeline_point(end),
                    self.max_timeline_points,
                    timeline_truncated,
                )
                snapshot_count += 1
                rss_bytes_peak = _max_optional(rss_bytes_peak, end.rss_bytes)
        wall_started_at, wall_ended_at = _wall_times_from_duration(
            start.captured_at,
            end.captured_at,
            completion.duration_ms,
        )
        duration_s = completion.duration_ms / 1000 if completion.duration_ms > 0 else None
        cpu_delta = _delta_float(start.process_cpu_time_s, end.process_cpu_time_s)
        read_delta = _delta_int(start.read_bytes, end.read_bytes)
        write_delta = _delta_int(start.write_bytes, end.write_bytes)
        net_rx_delta = _delta_int(start.net_rx_bytes, end.net_rx_bytes)
        net_tx_delta = _delta_int(start.net_tx_bytes, end.net_tx_bytes)
        cpu_avg_cores = _rate(cpu_delta, duration_s)
        normalized_timeline = _relative_timeline(timeline)
        return ToolRuntimeSample(
            event_id=completion.event_id,
            tool_call_id=completion.tool_call_id,
            tool_name=completion.tool_name,
            operation=operation,
            started_at=wall_started_at,
            ended_at=wall_ended_at,
            duration_ms=completion.duration_ms,
            monitor_duration_ms=max(0, int((end.monotonic_s - start.monotonic_s) * 1000)),
            monitor_start_wall_s=start.captured_at,
            monitor_end_wall_s=end.captured_at,
            monitor_start_monotonic_s=start.monotonic_s if start.available else None,
            monitor_end_monotonic_s=end.monotonic_s if end.available else None,
            cpu_time_delta_s=cpu_delta,
            rss_bytes_before=start.rss_bytes,
            rss_bytes_after=end.rss_bytes,
            read_bytes_delta=read_delta,
            write_bytes_delta=write_delta,
            net_rx_bytes_delta=net_rx_delta,
            net_tx_bytes_delta=net_tx_delta,
            ctx_switches_delta=_delta_int(start.ctx_switches, end.ctx_switches),
            rss_bytes_peak=rss_bytes_peak,
            cpu_utilization_avg_cores=cpu_avg_cores,
            cpu_utilization_avg_pct=None if cpu_avg_cores is None else cpu_avg_cores * 100,
            disk_read_bytes_per_s=_rate(read_delta, duration_s),
            disk_write_bytes_per_s=_rate(write_delta, duration_s),
            net_rx_bytes_per_s=_rate(net_rx_delta, duration_s),
            net_tx_bytes_per_s=_rate(net_tx_delta, duration_s),
            sampling_interval_ms=int(self.poll_interval_s * 1000),
            sampling_point_count=snapshot_count,
            sampling_quality=_sampling_quality(
                start,
                end,
                snapshot_count=snapshot_count,
                used_latest_snapshot=used_latest_snapshot,
                duration_ms=completion.duration_ms,
                poll_interval_s=self.poll_interval_s,
            ),
            resource_timeline=normalized_timeline,
            resource_timeline_truncated=timeline_truncated,
            resource_class=resource_class,
            target_pid=end.target_pid if end.target_pid is not None else start.target_pid,
            process_count_before=start.process_count,
            process_count_after=end.process_count,
            attribution_status=_attribution_status(start, end),
            monitor_source=end.source if end.available else start.source,
        )

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def bind_scope(self, tool_call_id: str | None, scope: ResourceScope) -> None:
        if tool_call_id is None:
            return
        with self._lock:
            active = self._pop_by_tool_call_id(tool_call_id)
            if active is None:
                return
            request = active.request.model_copy(update={"resource_scope": scope})
            snapshot = self.sampler.snapshot(request.resource_scope)
            if not snapshot.available and active.latest_snapshot.available:
                self._active[self._key(active.request.tool_call_id, active.request.event_id)] = active
                return
            self._active[self._key(request.tool_call_id, request.event_id)] = _ActiveTool(
                request=request,
                snapshot=snapshot,
                latest_snapshot=snapshot,
                rss_bytes_peak=snapshot.rss_bytes,
                timeline=[_timeline_point(snapshot)],
                snapshot_count=1,
                timeline_truncated=False,
                resource_class=active.resource_class,
                operation=active.operation,
            )

    def _pop_by_tool_call_id(self, tool_call_id: str) -> _ActiveTool | None:
        for key, active in list(self._active.items()):
            if active.request.tool_call_id == tool_call_id:
                self._active.pop(key, None)
                return active
        return None

    def _pop_unique_by_tool_name(self, tool_name: str) -> _ActiveTool | None:
        matches = [(key, active) for key, active in self._active.items() if active.request.tool_name == tool_name]
        if len(matches) != 1:
            return None
        key, active = matches[0]
        self._active.pop(key, None)
        return active

    def _poll_active(self) -> None:
        while True:
            time.sleep(self.poll_interval_s)
            with self._lock:
                items = list(self._active.items())
            for key, active in items:
                if active.request.resource_scope is None:
                    continue
                snapshot = self.sampler.snapshot(active.request.resource_scope)
                if not snapshot.available:
                    continue
                with self._lock:
                    current = self._active.get(key)
                    if current is not active:
                        continue
                    timeline, timeline_truncated = _append_timeline(
                        current.timeline,
                        _timeline_point(snapshot),
                        self.max_timeline_points,
                        current.timeline_truncated,
                    )
                    self._active[key] = _ActiveTool(
                        request=current.request,
                        snapshot=current.snapshot,
                        latest_snapshot=snapshot,
                        rss_bytes_peak=_max_optional(current.rss_bytes_peak, snapshot.rss_bytes),
                        timeline=timeline,
                        snapshot_count=current.snapshot_count + 1,
                        timeline_truncated=timeline_truncated,
                        resource_class=current.resource_class,
                        operation=current.operation,
                    )

    @staticmethod
    def _key(tool_call_id: str | None, event_id: str) -> str:
        return tool_call_id or event_id


def _delta_int(start: int | None, end: int | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, end - start)


def _delta_float(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, end - start)


def _rate(delta: float | int | None, duration_s: float | None) -> float | None:
    if delta is None or duration_s is None or duration_s <= 0:
        return None
    return max(0.0, float(delta) / duration_s)


def _attribution_status(start: ResourceSnapshot, end: ResourceSnapshot) -> str:
    if start.target_pid is None and end.target_pid is None:
        return "unattributed"
    if start.source == "cgroup-v2" or end.source == "cgroup-v2":
        return "cgroup-v2"
    if start.available and end.available:
        return "pid"
    return "pid-unavailable"


def _wall_times_from_duration(started_at: float, ended_at: float, duration_ms: int) -> tuple[float, float]:
    duration_s = max(0.0, duration_ms / 1000)
    if duration_s <= 0:
        return started_at, ended_at
    if ended_at < started_at:
        ended_at = started_at
    if ended_at - started_at < duration_s:
        started_at = ended_at - duration_s
    return started_at, ended_at


def _max_optional(left: int | None, right: int | None) -> int | None:
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _timeline_point(snapshot: ResourceSnapshot) -> dict[str, Any]:
    return {
        "ts": snapshot.captured_at,
        "cpu_time_s": snapshot.process_cpu_time_s,
        "rss_bytes": snapshot.rss_bytes,
        "read_bytes": snapshot.read_bytes,
        "write_bytes": snapshot.write_bytes,
        "net_rx_bytes": snapshot.net_rx_bytes,
        "net_tx_bytes": snapshot.net_tx_bytes,
        "ctx_switches": snapshot.ctx_switches,
        "process_count": snapshot.process_count,
        "available": snapshot.available,
        "source": snapshot.source,
    }


def _relative_timeline(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not points:
        return []
    base = points[0]
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for point in points:
        elapsed_s = _timeline_delta_float(base.get("ts"), point.get("ts"))
        interval_s = None if prev is None else _timeline_delta_float(prev.get("ts"), point.get("ts"))
        read_delta = _timeline_counter_delta(base.get("read_bytes"), point.get("read_bytes"))
        write_delta = _timeline_counter_delta(base.get("write_bytes"), point.get("write_bytes"))
        net_rx_delta = _timeline_counter_delta(base.get("net_rx_bytes"), point.get("net_rx_bytes"))
        net_tx_delta = _timeline_counter_delta(base.get("net_tx_bytes"), point.get("net_tx_bytes"))
        ctx_delta = _timeline_counter_delta(base.get("ctx_switches"), point.get("ctx_switches"))
        point_read_delta = None if prev is None else _timeline_counter_delta(prev.get("read_bytes"), point.get("read_bytes"))
        point_write_delta = None if prev is None else _timeline_counter_delta(prev.get("write_bytes"), point.get("write_bytes"))
        point_net_rx_delta = None if prev is None else _timeline_counter_delta(prev.get("net_rx_bytes"), point.get("net_rx_bytes"))
        point_net_tx_delta = None if prev is None else _timeline_counter_delta(prev.get("net_tx_bytes"), point.get("net_tx_bytes"))
        out.append(
            {
                "ts": point.get("ts"),
                "elapsed_ms": None if elapsed_s is None else int(elapsed_s * 1000),
                "cpu_time_delta_s": _timeline_counter_delta(base.get("cpu_time_s"), point.get("cpu_time_s")),
                "rss_bytes": point.get("rss_bytes"),
                "read_bytes_delta": read_delta,
                "write_bytes_delta": write_delta,
                "net_rx_bytes_delta": net_rx_delta,
                "net_tx_bytes_delta": net_tx_delta,
                "ctx_switches_delta": ctx_delta,
                "read_bytes_per_s": _rate(point_read_delta, interval_s),
                "write_bytes_per_s": _rate(point_write_delta, interval_s),
                "net_rx_bytes_per_s": _rate(point_net_rx_delta, interval_s),
                "net_tx_bytes_per_s": _rate(point_net_tx_delta, interval_s),
                "process_count": point.get("process_count"),
                "available": point.get("available"),
                "source": point.get("source"),
            }
        )
        prev = point
    return out


def _timeline_counter_delta(start: Any, end: Any) -> float | int | None:
    if start is None or end is None:
        return None
    try:
        delta = float(end) - float(start)
    except (TypeError, ValueError):
        return None
    if delta < 0:
        return 0
    if isinstance(start, int) and isinstance(end, int):
        return int(delta)
    return delta


def _timeline_delta_float(start: Any, end: Any) -> float | None:
    if start is None or end is None:
        return None
    try:
        return max(0.0, float(end) - float(start))
    except (TypeError, ValueError):
        return None


def _append_timeline(
    timeline: list[dict[str, Any]],
    point: dict[str, Any],
    max_points: int,
    truncated: bool,
) -> tuple[list[dict[str, Any]], bool]:
    if len(timeline) >= max_points:
        return timeline, True
    return [*timeline, point], truncated


def _sampling_quality(
    start: ResourceSnapshot,
    end: ResourceSnapshot,
    *,
    snapshot_count: int,
    used_latest_snapshot: bool,
    duration_ms: int,
    poll_interval_s: float,
) -> str:
    if start.target_pid is None and end.target_pid is None:
        return "unattributed"
    if not start.available and not end.available:
        return "unavailable"
    if used_latest_snapshot:
        return "partial"
    if snapshot_count < 2 or duration_ms < int(poll_interval_s * 1000):
        return "low"
    return "ok"
