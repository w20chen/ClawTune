from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from agent_scheduler.contracts.models import ResourceScope, ToolBeforeRequest, ToolCompletedEvent
from agent_scheduler.identity import correlation_key, owners_compatible
from agent_scheduler.monitoring.process import ProcessResourceSampler, ResourceSnapshot
from agent_scheduler.tool_resource_commands import operation_from_request


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
        self._active: dict[tuple[str | None, ...], _ActiveTool] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._poller = threading.Thread(target=self._poll_active, daemon=True)
        self._poller.start()

    def begin(self, request: ToolBeforeRequest, resource_class: str) -> None:
        key = correlation_key(request)
        snapshot = self.sampler.snapshot(request.resource_scope, net_mode="reset")
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
                operation=operation_from_request(request),
            )

    def complete(self, completion: ToolCompletedEvent) -> ToolRuntimeSample:
        key = correlation_key(completion)
        with self._lock:
            active = self._active.pop(key, None)
            if active is None and completion.tool_call_id is not None:
                active = self._pop_by_tool_call_id(
                    completion.tool_call_id,
                    completion.runtime_id,
                    owner=completion,
                )
            if active is None and completion.tool_call_id is None:
                active = self._pop_unique_by_tool_name(
                    completion.tool_name,
                    completion.runtime_id,
                    owner=completion,
                )
        completion_scope = completion.resource_scope
        if completion_scope is None and active is not None:
            completion_scope = active.request.resource_scope
        end = self.sampler.snapshot(completion_scope, net_mode="read")
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
        # Defensive cross-source guard: if the start baseline and the end
        # snapshot come from different measurement sources (e.g. bind_scope
        # never ran, so the baseline is still the shared container cgroup
        # while the completion scope resolved to a per-execution PID/cgroup),
        # their counters live on different cumulative epochs and must not be
        # subtracted.  Rebase the baseline to the first timeline sample that
        # shares the end snapshot's source; if none exists, fall back to an
        # empty baseline so the deltas read as unavailable instead of garbage.
        if start.source != end.source:
            rebased_point = _first_timeline_point_of_source(
                timeline, end.source, before=end.captured_at
            )
            if rebased_point is not None:
                start = _snapshot_from_point(
                    rebased_point, target_pid=end.target_pid
                )
            else:
                start = _snapshot_from_point(
                    {
                        "ts": end.captured_at,
                        "cpu_time_s": None,
                        "rss_bytes": None,
                        "read_bytes": None,
                        "write_bytes": None,
                        "net_rx_bytes": None,
                        "net_tx_bytes": None,
                        "ctx_switches": None,
                        "process_count": None,
                        "available": False,
                        "source": end.source,
                    },
                    target_pid=end.target_pid,
                )

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

    def stop(self) -> None:
        self._stop.set()
        if self._poller is not threading.current_thread():
            self._poller.join(timeout=max(0.1, self.poll_interval_s * 2))

    def bind_scope(
        self,
        tool_call_id: str | None,
        scope: ResourceScope,
        runtime_id: str | None = None,
        *,
        owner: ToolBeforeRequest | ToolCompletedEvent | None = None,
    ) -> bool:
        if tool_call_id is None:
            return False
        with self._lock:
            active = self._pop_by_tool_call_id(
                tool_call_id,
                runtime_id,
                owner=owner,
            )
            if active is None:
                return False
            current_scope = active.request.resource_scope
            if (
                current_scope is not None
                and current_scope.kind == scope.kind
                and current_scope.pid == scope.pid
                and current_scope.cgroup_path == scope.cgroup_path
                and current_scope.source == scope.source
                and current_scope.attribution_source == scope.attribution_source
            ):
                self._active[correlation_key(active.request)] = active
                return True
            request = active.request.model_copy(update={"resource_scope": scope})
            # Reset the per-process network baseline on a scope rebase so the
            # new target's counters only count traffic from this point on.
            snapshot = self.sampler.snapshot(request.resource_scope, net_mode="reset")
            if not snapshot.available and active.latest_snapshot.available:
                self._active[correlation_key(active.request)] = active
                return False
            if snapshot.available and active.snapshot.available:
                # The scope guard above only returns early when the target is
                # unchanged, so reaching here means the new scope measures a
                # DIFFERENT target (e.g. shared container cgroup -> per-exec
                # host PID, or shared container cgroup -> per-execution
                # cgroup).  The old baseline's counters live on a different
                # cumulative epoch (container cgroup usage_usec vs per-process
                # psutil cpu_times) and must not be subtracted from the new
                # scope's samples.  Rebase immediately while the process is
                # alive; coverage will honestly report the late start.
                self._active[correlation_key(request)] = _ActiveTool(
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
                return True
            timeline = list(active.timeline)
            timeline_truncated = active.timeline_truncated
            snapshot_count = active.snapshot_count
            rss_bytes_peak = active.rss_bytes_peak
            if snapshot.available:
                timeline, timeline_truncated = _append_timeline(
                    timeline,
                    _timeline_point(snapshot),
                    self.max_timeline_points,
                    timeline_truncated,
                )
                snapshot_count += 1
                rss_bytes_peak = _max_optional(rss_bytes_peak, snapshot.rss_bytes)
            start_snapshot = active.snapshot if active.snapshot.available else snapshot
            self._active[correlation_key(request)] = _ActiveTool(
                request=request,
                snapshot=start_snapshot,
                latest_snapshot=snapshot,
                rss_bytes_peak=rss_bytes_peak,
                timeline=timeline,
                snapshot_count=snapshot_count,
                timeline_truncated=timeline_truncated,
                resource_class=active.resource_class,
                operation=active.operation,
            )
            return snapshot.available

    def _pop_by_tool_call_id(
        self,
        tool_call_id: str,
        runtime_id: str | None,
        *,
        owner: ToolBeforeRequest | ToolCompletedEvent | None = None,
    ) -> _ActiveTool | None:
        for key, active in list(self._active.items()):
            if (
                active.request.tool_call_id == tool_call_id
                and active.request.runtime_id == runtime_id
                and (owner is None or owners_compatible(active.request, owner))
            ):
                self._active.pop(key, None)
                return active
        return None

    def _pop_unique_by_tool_name(
        self,
        tool_name: str,
        runtime_id: str | None,
        *,
        owner: ToolBeforeRequest | ToolCompletedEvent | None = None,
    ) -> _ActiveTool | None:
        matches = [
            (key, active)
            for key, active in self._active.items()
            if active.request.tool_name == tool_name
            and active.request.runtime_id == runtime_id
            and (owner is None or owners_compatible(active.request, owner))
        ]
        if len(matches) != 1:
            return None
        key, active = matches[0]
        self._active.pop(key, None)
        return active

    def _poll_active(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            with self._lock:
                items = list(self._active.items())
            for key, active in items:
                if active.request.resource_scope is None:
                    continue
                snapshot = self.sampler.snapshot(
                    active.request.resource_scope, net_mode="ignore"
                )
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
                        snapshot=current.snapshot if current.snapshot.available else snapshot,
                        latest_snapshot=snapshot,
                        rss_bytes_peak=_max_optional(current.rss_bytes_peak, snapshot.rss_bytes),
                        timeline=timeline,
                        snapshot_count=current.snapshot_count + 1,
                        timeline_truncated=timeline_truncated,
                        resource_class=current.resource_class,
                        operation=current.operation,
                    )

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
        if prev is not None and point.get("source") != prev.get("source"):
            # Measurement source changed (e.g. shared container cgroup ->
            # per-execution process tree).  Counters from different sources
            # live on different cumulative epochs, so start a fresh segment
            # baseline instead of subtracting a foreign base.
            base = point
        # The first sample of a segment (list head or a source change) has no
        # same-source predecessor: its point-to-point per-second rates must be
        # unavailable rather than subtracted from the previous foreign-source
        # sample (which produced garbage rates at source boundaries).
        segment_origin = prev is None or point.get("source") != prev.get("source")
        elapsed_s = _timeline_delta_float(base.get("ts"), point.get("ts"))
        interval_s = None if prev is None else _timeline_delta_float(prev.get("ts"), point.get("ts"))
        read_delta = _timeline_counter_delta(base.get("read_bytes"), point.get("read_bytes"))
        write_delta = _timeline_counter_delta(base.get("write_bytes"), point.get("write_bytes"))
        net_rx_delta = _timeline_counter_delta(base.get("net_rx_bytes"), point.get("net_rx_bytes"))
        net_tx_delta = _timeline_counter_delta(base.get("net_tx_bytes"), point.get("net_tx_bytes"))
        ctx_delta = _timeline_counter_delta(base.get("ctx_switches"), point.get("ctx_switches"))
        point_read_delta = None if segment_origin else _timeline_counter_delta(prev.get("read_bytes"), point.get("read_bytes"))
        point_write_delta = None if segment_origin else _timeline_counter_delta(prev.get("write_bytes"), point.get("write_bytes"))
        point_net_rx_delta = None if segment_origin else _timeline_counter_delta(prev.get("net_rx_bytes"), point.get("net_rx_bytes"))
        point_net_tx_delta = None if segment_origin else _timeline_counter_delta(prev.get("net_tx_bytes"), point.get("net_tx_bytes"))
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


def _first_timeline_point_of_source(
    timeline: list[dict[str, Any]],
    source: str,
    *,
    before: float | None = None,
) -> dict[str, Any] | None:
    """Return the earliest available timeline sample for a measurement source.

    When ``before`` is given, only samples captured strictly before that
    timestamp are considered (used to avoid rebasing onto the end snapshot
    itself when no earlier sample of that source exists).
    """
    for point in timeline:
        if point.get("source") != source or not point.get("available"):
            continue
        if before is not None:
            ts = point.get("ts")
            if not isinstance(ts, (int, float)) or ts >= before:
                continue
        return point
    return None


def _snapshot_from_point(
    point: dict[str, Any],
    *,
    target_pid: int | None,
) -> ResourceSnapshot:
    """Rebuild a ResourceSnapshot from a raw timeline point (see _timeline_point)."""
    ts = point.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) else 0.0
    return ResourceSnapshot(
        captured_at=ts,
        monotonic_s=ts,
        process_cpu_time_s=point.get("cpu_time_s"),
        rss_bytes=point.get("rss_bytes"),
        read_bytes=point.get("read_bytes"),
        write_bytes=point.get("write_bytes"),
        net_rx_bytes=point.get("net_rx_bytes"),
        net_tx_bytes=point.get("net_tx_bytes"),
        ctx_switches=point.get("ctx_switches"),
        target_pid=target_pid,
        process_count=point.get("process_count"),
        available=bool(point.get("available")),
        source=point.get("source") or "unknown",
    )


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
