"""Default eBPF resource windows for OpenClaw tool calls.

eBPF is preferred; dedicated cgroup windows provide an explicit fallback
when the collector fails.  Procfs is used only to resolve an already authenticated PID to its
cgroup identity and to seed the set of descendants which existed when the
window opened. Sampling gaps alone never trigger a different backend.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from clawtune_sidecar.contracts.models import ResourceScope


_MAX_SAMPLE_GAP_NS = 150_000_000
_CPU_WINDOW_NS = 500_000_000
_RSS_BIN_NS = 20_000_000
_COUNTER_EVENT_TYPES = {"perf", "exec_boundary", "exit_boundary"}


def _unavailable_metric(reason: str, *, measurement: str) -> dict[str, Any]:
    return {
        "available": False,
        "eligible": False,
        "reason": reason,
        "measurement": measurement,
    }


def unavailable_observation(
    reason: str,
    *,
    attribution: str = "unattributed",
    started_ns: int | None = None,
    ended_ns: int | None = None,
) -> dict[str, Any]:
    start = started_ns if started_ns is not None else time.monotonic_ns()
    end = ended_ns if ended_ns is not None else start
    return {
        "schema": "tool_resource_observation_v1",
        "backend": "ebpf",
        "fallback_used": False,
        "scope": "none",
        "attribution": attribution,
        "window": {
            "clock": "linux_monotonic",
            "requested_start_ns": str(start),
            "requested_end_ns": str(max(start, end)),
            "observed_start_ns": None,
            "observed_end_ns": None,
            "coverage_ratio": None,
        },
        "metrics": {
            "cpu_time": _unavailable_metric(reason, measurement="ebpf_task_cpu_time"),
            "cpu_peak": _unavailable_metric(reason, measurement="ebpf_task_cpu_500ms_peak"),
            "memory_peak": _unavailable_metric(reason, measurement="ebpf_sampled_distinct_mm_rss"),
            "disk_io": _unavailable_metric(reason, measurement="ebpf_task_io_accounting"),
            "network_io": _unavailable_metric("ebpf_network_unavailable", measurement="ebpf_tcp_send_recv_bytes"),
        },
        "unavailable_reason": reason,
    }


def _is_shared(scope: ResourceScope) -> bool:
    return scope.attribution_source in {
        "shared-runtime-process",
        "shared-sandbox-container",
    }


def _same_scope(left: ResourceScope | None, right: ResourceScope | None) -> bool:
    if left is None or right is None:
        return left is right
    return all(
        getattr(left, field) == getattr(right, field)
        for field in (
            "kind", "pid", "root_pid", "cgroup_path", "root_starttime_ticks",
            "pid_namespace_inode", "source", "attribution_source", "include_children",
        )
    )


def _is_cgroup_root(path: Path) -> bool:
    return path.as_posix().rstrip("/") in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}


def _cgroup_for_pid(pid: int) -> Path | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("0::"):
            continue
        relative = line[3:].strip()
        path = Path("/sys/fs/cgroup") / relative.lstrip("/")
        return path
    return None


def _resolved_cgroup(scope: ResourceScope) -> Path | None:
    candidates: list[Path] = []
    if scope.cgroup_path:
        candidates.append(Path(scope.cgroup_path))
    root_pid = scope.root_pid or scope.pid
    if root_pid:
        resolved = _cgroup_for_pid(root_pid)
        if resolved is not None:
            candidates.append(resolved)
    for path in candidates:
        try:
            if path.is_dir() and not _is_cgroup_root(path):
                return path
        except OSError:
            continue
    return None


def _pid_starttime_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(text[text.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _existing_descendants(root_pid: int) -> set[int]:
    """Return identities only; no resource counter is read here."""
    found = {root_pid}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        task_dir = Path(f"/proc/{pid}/task")
        try:
            tids = list(task_dir.iterdir())
        except OSError:
            continue
        for tid_dir in tids:
            try:
                children = (tid_dir / "children").read_text(encoding="utf-8").split()
            except OSError:
                continue
            for value in children:
                try:
                    child = int(value)
                except ValueError:
                    continue
                if child > 0 and child not in found:
                    found.add(child)
                    pending.append(child)
    return found


@dataclass
class _KernelWindow:
    source: Any
    lease_id: int
    buffer: Any
    bpf: Any
    root_pid: int
    root_starttime_ticks: int | None
    started_ns: int
    initial_pids: set[int]
    shared: bool
    loss_before: dict[str, int]
    network: Any | None = None
    network_before: tuple[int, int] | None = None
    network_started_ns: int | None = None

    @classmethod
    def open(cls, scope: ResourceScope) -> "_KernelWindow":
        from tool_resource import telemetry

        root_pid = int(scope.root_pid or scope.pid or 0)
        if root_pid <= 0:
            raise RuntimeError("ebpf_root_pid_unavailable")
        observed_starttime = _pid_starttime_ticks(root_pid)
        claimed = scope.root_starttime_ticks
        if claimed is not None and observed_starttime is not None and int(claimed) != observed_starttime:
            raise RuntimeError("ebpf_root_pid_reused")
        cgroup = _resolved_cgroup(scope)
        if cgroup is None:
            raise RuntimeError("ebpf_cgroup_identity_unavailable")
        bcc = telemetry._ensure_bcc_importable()
        source, lease_id, buffer = telemetry._acquire_shared_bpf_source(
            bcc.BPF,
            bcc.PerfType,
            bcc.PerfSWConfig,
            cgroup_ids={int(cgroup.stat().st_ino)},
            pid_namespace_ids=set(),
        )
        try:
            return cls(
                source=source,
                lease_id=lease_id,
                buffer=buffer,
                bpf=source.bpf,
                root_pid=root_pid,
                root_starttime_ticks=observed_starttime,
                started_ns=time.monotonic_ns(),
                initial_pids=_existing_descendants(root_pid) if scope.include_children else {root_pid},
                shared=_is_shared(scope),
                loss_before=telemetry._loss_counts(source.bpf),
            )
        except BaseException:
            source.release(lease_id)
            raise

    def finish(self, ended_ns: int, *, started_ns: int | None = None) -> dict[str, Any]:
        from tool_resource import telemetry

        requested_start_ns = self.started_ns if started_ns is None else int(started_ns)

        # Events carry their kernel timestamp before ring delivery.  Drain only
        # delivery; the requested window still ends at ended_ns.
        time.sleep(0.03)
        loss_after: dict[str, int] | None = None
        try:
            if self.source.poll_error is not None:
                return unavailable_observation(
                    f"ebpf_poller_failed:{type(self.source.poll_error).__name__}",
                    attribution="shared_scope" if self.shared else "exclusive_process_tree",
                    started_ns=requested_start_ns,
                    ended_ns=ended_ns,
                )
            with self.buffer.lock:
                events = list(self.buffer.events)
            loss_after = telemetry._loss_counts(self.bpf)
        except Exception as exc:
            return unavailable_observation(
                f"ebpf_finish_failed:{type(exc).__name__}",
                attribution="shared_scope" if self.shared else "exclusive_process_tree",
                started_ns=requested_start_ns,
                ended_ns=ended_ns,
            )
        finally:
            self.source.release(self.lease_id)
        assert loss_after is not None
        loss = {
            key: max(0, int(loss_after.get(key, 0)) - int(self.loss_before.get(key, 0)))
            for key in set(self.loss_before) | set(loss_after)
        }
        if any(loss.values()):
            result = unavailable_observation(
                "ebpf_event_loss",
                attribution="shared_scope" if self.shared else "exclusive_process_tree",
                    started_ns=requested_start_ns,
                ended_ns=ended_ns,
            )
            result["loss"] = loss
            return result
        if self.root_starttime_ticks is not None:
            current_starttime = _pid_starttime_ticks(self.root_pid)
            if current_starttime is not None and current_starttime != self.root_starttime_ticks:
                return unavailable_observation(
                    "ebpf_root_pid_reused",
                    attribution="shared_scope" if self.shared else "exclusive_process_tree",
                    started_ns=requested_start_ns,
                    ended_ns=ended_ns,
                )
        selected, selected_pids = self._select_lineage(events, ended_ns, requested_start_ns)
        network_delta: tuple[int, int] | None = None
        if self.network is not None and self.network_before is not None:
            end_rx, end_tx = self.network.rx_tx_for(selected_pids)
            network_delta = (
                max(0, end_rx - self.network_before[0]),
                max(0, end_tx - self.network_before[1]),
            )
        result = _reduce_events(
            selected,
            started_ns=requested_start_ns,
            ended_ns=ended_ns,
            shared=self.shared,
            network_delta=network_delta,
        )
        if network_delta is not None:
            result["metrics"]["network_io"].update(
                eligible=False, reason="collector_window_only",
                window_start_ns=str(self.network_started_ns or self.started_ns),
                window_end_ns=str(time.monotonic_ns()),
            )
        return result

    def _select_lineage(
        self, events: list[Mapping[str, Any]], ended_ns: int, started_ns: int
    ) -> tuple[list[dict[str, Any]], set[int]]:
        pids = set(self.initial_pids)
        selected: list[dict[str, Any]] = []
        for raw in sorted(events, key=lambda row: int(row.get("ts_ns", 0))):
            row = dict(raw)
            ts = int(row.get("ts_ns", 0) or 0)
            if ts > ended_ns:
                continue
            host_pid = int(row.get("host_pid", 0) or 0)
            if row.get("type") == "fork" and host_pid in pids:
                child = int(row.get("child_host_pid", 0) or 0)
                if child > 0:
                    pids.add(child)
            if host_pid in pids and ts >= started_ns:
                selected.append(row)
        return selected, pids


def _counter_delta_by_tid(
    events: list[Mapping[str, Any]], field: str
) -> tuple[int | None, dict[int, list[tuple[int, int]]]]:
    per_tid: dict[int, list[tuple[int, int]]] = {}
    forked: set[int] = set()
    for row in events:
        if row.get("type") == "fork":
            child = int(row.get("child_host_tid", 0) or 0)
            if child > 0:
                forked.add(child)
            continue
        if row.get("type") not in _COUNTER_EVENT_TYPES:
            continue
        tid = int(row.get("host_tid", 0) or 0)
        value = row.get(field)
        if tid <= 0 or not isinstance(value, int) or value < 0:
            continue
        per_tid.setdefault(tid, []).append((int(row.get("ts_ns", 0)), value))
    if not per_tid:
        return None, per_tid
    total = 0
    usable = False
    for tid, points in per_tid.items():
        points.sort()
        if any(b < a for (_, a), (_, b) in zip(points, points[1:])):
            return None, per_tid
        baseline = 0 if tid in forked else points[0][1]
        if len(points) >= 2 or tid in forked:
            total += max(0, points[-1][1] - baseline)
            usable = True
    return (total if usable else None), per_tid


def _edge_coverage(
    events: list[Mapping[str, Any]], start: int, end: int
) -> tuple[float | None, int | None, int | None, int]:
    timestamps = sorted({int(row.get("ts_ns", 0)) for row in events if start <= int(row.get("ts_ns", 0)) <= end})
    if not timestamps or end <= start:
        return None, None, None, end - start
    observed_start, observed_end = timestamps[0], timestamps[-1]
    gaps = [observed_start - start, end - observed_end]
    gaps.extend(b - a for a, b in zip(timestamps, timestamps[1:]))
    overlap = max(0, observed_end - observed_start)
    return min(1.0, overlap / (end - start)), observed_start, observed_end, max(gaps, default=0)


def _metric_window_complete(
    events: list[Mapping[str, Any]], field: str, start: int, end: int
) -> bool:
    metric_events = [
        row for row in events
        if row.get("type") in _COUNTER_EVENT_TYPES
        and isinstance(row.get(field), int) and int(row[field]) >= 0
    ]
    ratio, observed_start, observed_end, max_gap = _edge_coverage(metric_events, start, end)
    return (
        ratio is not None and ratio >= 0.8
        and observed_start is not None
        and observed_end is not None
        and max_gap <= _MAX_SAMPLE_GAP_NS
    )


def _counter_window_complete(events, field, start, end):
    """Validate each task lifetime separately; dense peers cannot fill its gaps."""
    tids = {}
    forks = {int(e.get("child_host_tid", 0)) for e in events if e.get("type") == "fork"}
    for row in events:
        if row.get("type") in _COUNTER_EVENT_TYPES and isinstance(row.get(field), int):
            tids.setdefault(int(row.get("host_tid", 0)), []).append(row)
    if not tids or forks - set(tids):
        return False
    for tid, rows in tids.items():
        rows.sort(key=lambda row: row["ts_ns"])
        if any(b[field] < a[field] for a, b in zip(rows, rows[1:])):
            return False
        first, last = rows[0], rows[-1]
        if len(rows) < 2 and tid not in forks:
            return False
        if tid not in forks and first["ts_ns"] - start > _MAX_SAMPLE_GAP_NS:
            return False
        if last["type"] != "exit_boundary" and end - last["ts_ns"] > _MAX_SAMPLE_GAP_NS:
            return False
        # A new task or an exec baseline plus exit closes a cumulative
        # lifetime even when the task sleeps. Perf-only evidence needs density.
        if not ((tid in forks or (first["type"] == "exec_boundary" and first["ts_ns"] == start))
                and last["type"] == "exit_boundary"):
            if not _metric_window_complete(rows, field, start, end):
                return False
    return True


def _fallback_result(primary, fallback):
    reason = primary.get("unavailable_reason", "")
    if fallback is not None and reason.startswith("ebpf_"):
        fallback["fallback_reason"] = reason
        return fallback
    return primary


def _cpu_peak(per_tid: Mapping[int, list[tuple[int, int]]], start: int, end: int) -> float | None:
    if end - start < _CPU_WINDOW_NS:
        return None
    window_count = (end - start) // _CPU_WINDOW_NS
    windows = [0.0] * window_count
    for points in per_tid.values():
        for (ta, ca), (tb, cb) in zip(points, points[1:]):
            if tb <= ta or cb < ca or tb < start or ta > end:
                continue
            interval_start = max(start, ta)
            interval_end = min(end, tb)
            if interval_end <= interval_start:
                continue
            # Cumulative task CPU is sampled. Distribute each counter delta
            # over the wall interval it brackets, then integrate it into the
            # fixed 500 ms windows anchored at the requested action start.
            rate = (cb - ca) / (tb - ta)
            first = max(0, (interval_start - start) // _CPU_WINDOW_NS)
            last = min(window_count - 1, (interval_end - start - 1) // _CPU_WINDOW_NS)
            for idx in range(first, last + 1):
                window_start = start + idx * _CPU_WINDOW_NS
                window_end = window_start + _CPU_WINDOW_NS
                overlap = max(0, min(interval_end, window_end) - max(interval_start, window_start))
                windows[idx] += rate * overlap
    return max((cpu_ns / _CPU_WINDOW_NS for cpu_ns in windows), default=None)


def _rss_peak(events: list[Mapping[str, Any]]) -> tuple[int | None, int]:
    bins: dict[int, dict[int, int]] = {}
    count = 0
    for row in events:
        if row.get("type") not in _COUNTER_EVENT_TYPES:
            continue
        rss_pages = row.get("rss_pages")
        mm = row.get("mm_ptr")
        ts = row.get("ts_ns")
        if not isinstance(rss_pages, int) or rss_pages <= 0 or not isinstance(mm, int) or mm <= 0 or not isinstance(ts, int):
            continue
        bins.setdefault(ts // _RSS_BIN_NS, {})[mm] = rss_pages
        count += 1
    if count < 2:
        return None, count
    page_size = 4096
    try:
        from tool_resource.telemetry import PAGE
        page_size = int(PAGE)
    except Exception:
        pass
    return max(sum(by_mm.values()) for by_mm in bins.values()) * page_size, count


def _reduce_events(
    events: list[Mapping[str, Any]], *, started_ns: int, ended_ns: int, shared: bool,
    network_delta: tuple[int, int] | None = None,
) -> dict[str, Any]:
    attribution = "shared_scope" if shared else "exclusive_process_tree"
    if not events:
        return unavailable_observation(
            "insufficient_samples",
            attribution=attribution,
            started_ns=started_ns,
            ended_ns=ended_ns,
        )
    resource_events = [row for row in events if row.get("type") in _COUNTER_EVENT_TYPES]
    ratio, observed_start, observed_end, max_gap = _edge_coverage(resource_events, started_ns, ended_ns)
    cpu_ns, cpu_by_tid = _counter_delta_by_tid(events, "cpu_ns")
    read_bytes, _ = _counter_delta_by_tid(events, "io_read_bytes")
    write_bytes, _ = _counter_delta_by_tid(events, "io_write_bytes")
    rss_peak, rss_samples = _rss_peak(events)
    duration_s = (ended_ns - started_ns) / 1e9 if ended_ns > started_ns else 0.0
    full_window = (
        ratio is not None and ratio >= 0.8
        and max_gap <= _MAX_SAMPLE_GAP_NS
        and observed_start is not None
        and observed_end is not None
    )
    cpu_window = _metric_window_complete(events, "cpu_ns", started_ns, ended_ns)
    memory_window = _metric_window_complete(events, "rss_pages", started_ns, ended_ns)
    for mm in {row.get("mm_ptr") for row in resource_events if row.get("mm_ptr")}:
        rows = [row for row in resource_events if row.get("mm_ptr") == mm]
        timestamps = sorted(row["ts_ns"] for row in rows)
        if any(b - a > _MAX_SAMPLE_GAP_NS for a, b in zip(timestamps, timestamps[1:])):
            memory_window = False
    exclusive = not shared
    cpu_available = cpu_ns is not None
    cpu_total_window = _counter_window_complete(events, "cpu_ns", started_ns, ended_ns)
    cpu_eligible = cpu_available and cpu_total_window and exclusive and duration_s > 0
    cpu_reason = (
        "ok" if cpu_eligible else
        "shared_scope" if shared else
        "incomplete_counter_boundaries" if cpu_available and not cpu_total_window else
        "insufficient_samples"
    )
    peak = _cpu_peak(cpu_by_tid, started_ns, ended_ns)
    dense_cpu_profiles = all(
        all(tb - ta <= _MAX_SAMPLE_GAP_NS for (ta, _), (tb, _) in zip(points, points[1:]))
        for points in cpu_by_tid.values()
    )
    peak_eligible = peak is not None and cpu_window and cpu_total_window and dense_cpu_profiles and exclusive
    memory_available = rss_peak is not None
    memory_eligible = memory_available and memory_window and exclusive
    memory_reason = (
        "ok" if memory_eligible else
        "shared_scope" if shared else
        "sampling_gap" if memory_available and not memory_window else
        "insufficient_samples"
    )
    disk_available = read_bytes is not None and write_bytes is not None
    disk_window = all(_counter_window_complete(events, field, started_ns, ended_ns)
                      for field in ("io_read_bytes", "io_write_bytes"))
    disk_eligible = disk_available and disk_window and exclusive
    return {
        "schema": "tool_resource_observation_v1",
        "backend": "ebpf",
        "fallback_used": False,
        "scope": "process_tree",
        "attribution": attribution,
        "window": {
            "clock": "linux_monotonic",
            "requested_start_ns": str(started_ns),
            "requested_end_ns": str(ended_ns),
            "observed_start_ns": None if observed_start is None else str(observed_start),
            "observed_end_ns": None if observed_end is None else str(observed_end),
            "coverage_ratio": ratio,
            "max_sample_gap_ns": str(max_gap),
            "complete": full_window,
        },
        "metrics": {
            "cpu_time": {
                "available": cpu_available,
                "eligible": cpu_eligible,
                "reason": cpu_reason,
                "measurement": "ebpf_task_cpu_time",
                "value_seconds": None if cpu_ns is None else cpu_ns / 1e9,
                "average_cores": None if not cpu_eligible else cpu_ns / 1e9 / duration_s,
                "sample_count": sum(len(points) for points in cpu_by_tid.values()),
            },
            "cpu_peak": {
                "available": peak is not None,
                "eligible": peak_eligible,
                "reason": "ok" if peak_eligible else (
                    "shared_scope" if shared else
                    "sampling_gap" if peak is not None and not cpu_window else
                    "insufficient_window_or_samples"
                ),
                "measurement": "ebpf_task_cpu_500ms_peak",
                "value_cores": peak,
                "window_ms": 500,
            },
            "memory_peak": {
                "available": memory_available,
                "eligible": memory_eligible,
                "reason": memory_reason,
                "measurement": "ebpf_sampled_distinct_mm_rss",
                "value_bytes": rss_peak,
                "sample_count": rss_samples,
                "counter_exact": False,
            },
            "disk_io": {
                "available": disk_available,
                "eligible": disk_eligible,
                "reason": "ok" if disk_eligible else (
                    "shared_scope" if shared else
                    "sampling_gap" if disk_available and not disk_window else
                    "insufficient_samples"
                ),
                "measurement": "ebpf_task_io_accounting",
                "read_bytes": read_bytes,
                "write_bytes": write_bytes,
            },
            "network_io": (
                {
                    "available": True,
                    "eligible": exclusive,
                    "reason": "ok" if exclusive else "shared_scope",
                    "measurement": "ebpf_tcp_send_recv_bytes",
                    "read_bytes": network_delta[0],
                    "write_bytes": network_delta[1],
                }
                if network_delta is not None
                else _unavailable_metric("ebpf_network_unavailable", measurement="ebpf_tcp_send_recv_bytes")
            ),
        },
    }


@dataclass
class _Active:
    scope: ResourceScope | None
    window: Any | None
    started_ns: int
    unavailable_reason: str | None = None
    bound_late: bool = False
    fallback: Any | None = None


class EbpfToolCallMonitor:
    """Own per-tool leases on the process-wide eBPF source."""

    def __init__(self, window_factory: Callable[[ResourceScope], Any] | None = None) -> None:
        self._network: Any | None = None
        self._factory = window_factory or self._open_default
        self._active: dict[Any, _Active] = {}
        self._lock = threading.RLock()

    def _open_default(self, scope: ResourceScope) -> _KernelWindow:
        from clawtune_sidecar.monitoring.net_accounting import (
            ProcessNetAccounting,
            _pid_namespace_inode,
        )

        window = _KernelWindow.open(scope)
        try:
            root_pid = int(scope.root_pid or scope.pid or 0)
            inode = _pid_namespace_inode(root_pid) if root_pid > 0 else None
            if inode is not None:
                if self._network is None:
                    candidate = ProcessNetAccounting([inode])
                    self._network = candidate if candidate.available else None
                else:
                    self._network.add_namespace(inode)
            if self._network is not None:
                window.network = self._network
                window.network_before = self._network.rx_tx_for(window.initial_pids)
                window.network_started_ns = time.monotonic_ns()
        except Exception:
            window.network = None
            window.network_before = None
        return window

    def begin(self, key: Any, scope: ResourceScope | None) -> None:
        with self._lock:
            self._begin(key, scope)

    def _begin(self, key: Any, scope: ResourceScope | None) -> None:
        self.discard(key)
        started_ns = time.monotonic_ns()
        active = _Active(scope=scope, window=None, started_ns=started_ns)
        from clawtune_sidecar.monitoring.cgroup_fallback import CgroupFallbackWindow
        active.fallback = CgroupFallbackWindow.open(scope)
        if scope is None:
            active.unavailable_reason = "ebpf_scope_unavailable_at_start"
        else:
            try:
                active.window = self._factory(scope)
                active.started_ns = int(getattr(active.window, "started_ns", started_ns))
            except Exception as exc:
                active.unavailable_reason = _error_reason(exc)
        with self._lock:
            self._active[key] = active

    def bind_scope(self, key: Any, scope: ResourceScope) -> bool:
        with self._lock:
            active = self._active.get(key)
            if active is None:
                return False
            if active.window is not None:
                if not _same_scope(active.scope, scope):
                    # Start a new window for the authoritative target. Its
                    # timestamp determines whether it precedes the action.
                    self._begin(key, scope)
                    self._active[key].bound_late = True
                    return self._active[key].window is not None
                return True
            active.scope = scope
            active.bound_late = True
            from clawtune_sidecar.monitoring.cgroup_fallback import CgroupFallbackWindow
            active.fallback = CgroupFallbackWindow.open(scope)
            try:
                active.window = self._factory(scope)
                active.started_ns = int(getattr(active.window, "started_ns", time.monotonic_ns()))
                active.unavailable_reason = None
            except Exception as exc:
                active.unavailable_reason = _error_reason(exc)
            return active.window is not None

    def complete(
        self,
        key: Any,
        *,
        action_start_ns: int | None = None,
        action_end_ns: int | None = None,
    ) -> dict[str, Any]:
        ended_ns = time.monotonic_ns()
        with self._lock:
            active = self._active.pop(key, None)
        if active is None:
            return unavailable_observation("ebpf_window_not_started", ended_ns=ended_ns)
        fallback = active.fallback.finish() if active.fallback is not None else None
        if active.window is None:
            result = unavailable_observation(
                active.unavailable_reason or "ebpf_unavailable",
                attribution="shared_scope" if active.scope and _is_shared(active.scope) else "unattributed",
                started_ns=active.started_ns,
                ended_ns=ended_ns,
            )
            return _fallback_result(result, fallback)
        exact_window = (
            action_start_ns is not None
            and action_end_ns is not None
            and action_end_ns >= action_start_ns
            and active.started_ns <= action_start_ns <= action_end_ns <= ended_ns
        )
        try:
            result = active.window.finish(
                action_end_ns if exact_window else ended_ns,
                started_ns=action_start_ns if exact_window else None,
            )
        except Exception as exc:
            try:
                active.window.source.release(active.window.lease_id)
            except Exception:
                pass
            result = unavailable_observation(
                f"ebpf_finish_failed:{type(exc).__name__}:{str(exc)[:400]}",
                started_ns=active.started_ns, ended_ns=ended_ns,
            )
        if not exact_window:
            result["window"]["complete"] = False
            result["window"]["action_clock_unusable"] = True
            for metric in result.get("metrics", {}).values():
                if isinstance(metric, dict) and metric.get("eligible"):
                    metric["eligible"] = False
                    metric["reason"] = "action_clock_window_unusable"
        if active.bound_late and not exact_window:
            result["window"]["complete"] = False
            result["window"]["late_scope_binding"] = True
            for metric in result.get("metrics", {}).values():
                if isinstance(metric, dict) and metric.get("available"):
                    metric["eligible"] = False
                    metric["reason"] = "scope_bound_after_action_start"
        return _fallback_result(result, fallback)

    def discard(self, key: Any) -> None:
        with self._lock:
            active = self._active.pop(key, None)
        if active is not None and active.window is not None:
            try:
                active.window.source.release(active.window.lease_id)
            except Exception:
                pass

    def stop(self) -> None:
        with self._lock:
            keys = list(self._active)
        for key in keys:
            self.discard(key)
        if self._network is not None:
            self._network.close()
            self._network = None


def _error_reason(exc: Exception) -> str:
    message = str(exc).strip()
    if message.startswith("ebpf_") and " " not in message:
        return message
    return f"ebpf_start_failed:{type(exc).__name__}:{message[:400]}"


def metric(observation: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    metrics = observation.get("metrics") if isinstance(observation, Mapping) else None
    value = metrics.get(name) if isinstance(metrics, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def observation_sample_fields(observation: Mapping[str, Any]) -> dict[str, Any]:
    cpu = metric(observation, "cpu_time")
    peak = metric(observation, "cpu_peak")
    memory = metric(observation, "memory_peak")
    disk = metric(observation, "disk_io")
    network = metric(observation, "network_io")
    window = observation.get("window") if isinstance(observation.get("window"), Mapping) else {}
    cpu_seconds = cpu.get("value_seconds") if cpu.get("available") else None
    duration_ns = _int_or_none(window.get("requested_end_ns"))
    start_ns = _int_or_none(window.get("requested_start_ns"))
    if window.get("kind") == "execution":
        start_ns = _int_or_none(window.get("observed_start_ns"))
        duration_ns = _int_or_none(window.get("observed_end_ns"))
    monitor_duration_ms = 0 if duration_ns is None or start_ns is None else max(0, (duration_ns - start_ns) // 1_000_000)
    return {
        "monitor_duration_ms": int(monitor_duration_ms),
        "monitor_start_monotonic_s": None if start_ns is None else start_ns / 1e9,
        "monitor_end_monotonic_s": None if duration_ns is None else duration_ns / 1e9,
        "cpu_time_delta_s": cpu_seconds,
        "cpu_utilization_avg_cores": cpu.get("average_cores") if cpu.get("eligible") else None,
        "cpu_utilization_avg_pct": None if not cpu.get("eligible") else float(cpu.get("average_cores")) * 100,
        "cpu_peak_cores": peak.get("value_cores") if peak.get("eligible") else None,
        "rss_bytes_peak": memory.get("value_bytes") if memory.get("available") else None,
        "read_bytes_delta": disk.get("read_bytes") if disk.get("available") else None,
        "write_bytes_delta": disk.get("write_bytes") if disk.get("available") else None,
        "net_rx_bytes_delta": network.get("read_bytes") if network.get("available") else None,
        "net_tx_bytes_delta": network.get("write_bytes") if network.get("available") else None,
        "sampling_point_count": int(cpu["sample_count"]) if cpu.get("sample_count") is not None else None,
        "sampling_quality": "ok" if window.get("complete") else ("partial" if cpu.get("available") or memory.get("available") else "unavailable"),
        "attribution_status": "pid" if observation.get("attribution") == "exclusive_process_tree" else ("shared-runtime" if observation.get("attribution") in {"shared_runtime", "shared_scope"} else "unattributed"),
        "monitor_source": "cgroup-v2-fallback" if observation.get("fallback_used") else "ebpf-task-lineage",
    }


def execution_observation(
    call: Mapping[str, Any],
    *,
    started_ns: int,
    ended_ns: int,
    clock: str = "sidecar_synthetic_duration_anchor",
) -> dict[str, Any] | None:
    """Build the call-level view from finalized execution eBPF evidence.

    Totals may be summed across causally disjoint owned clauses. Peaks require
    a single clause because scalar clause peaks cannot be composed without the
    aligned profiles retained in the full artifact.
    """
    clauses = call.get("clauses")
    if call.get("telemetry_quality") != "ok" or not isinstance(clauses, list) or not clauses:
        return None
    rows = [row for row in clauses if isinstance(row, Mapping)]
    if len(rows) != len(clauses):
        return None
    intervals = [(row.get("t_exec_ns"), row.get("t_end_ns")) for row in rows]
    comparable = clock == "linux_monotonic" and all(
        isinstance(lo, int) and isinstance(hi, int) and hi >= lo for lo, hi in intervals
    )
    observed_start = min(lo for lo, _ in intervals) if comparable else None
    observed_end = max(hi for _, hi in intervals) if comparable else None
    covered = 0
    cursor = started_ns
    if comparable:
        for lo, hi in sorted(intervals):
            left, right = max(started_ns, lo, cursor), min(ended_ns, hi)
            covered += max(0, right - left)
            cursor = max(cursor, right)
    complete = comparable and ended_ns > started_ns and covered == ended_ns - started_ns
    duration_s = max(0.0, (ended_ns - started_ns) / 1e9)
    cpu_values = [row.get("cpu_time_seconds") for row in rows]
    cpu_ok = all(
        isinstance(row.get("availability"), Mapping)
        and row["availability"].get("cpu_time") == "ok"
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
        for row, value in zip(rows, cpu_values)
    )
    cpu_seconds = sum(float(value) for value in cpu_values) if cpu_ok else None
    single = rows[0] if len(rows) == 1 else None
    peak_cpu = single.get("peak_cpu_cores") if single is not None else None
    peak_memory_mb = single.get("peak_memory_mb") if single is not None else None
    single_availability = single.get("availability") if single is not None else None
    peak_cpu_ok = (
        isinstance(single_availability, Mapping)
        and single_availability.get("cpu") == "ok"
        and isinstance(peak_cpu, (int, float))
        and math.isfinite(float(peak_cpu))
    )
    memory_ok = (
        isinstance(single_availability, Mapping)
        and single_availability.get("memory") == "ok"
        and isinstance(peak_memory_mb, (int, float))
        and math.isfinite(float(peak_memory_mb))
    )
    disk_ok = all(
        isinstance(row.get("availability"), Mapping)
        and row["availability"].get("disk_io") == "ok"
        and isinstance(row.get("disk_read_bytes"), int)
        and isinstance(row.get("disk_write_bytes"), int)
        for row in rows
    )
    read_bytes = sum(int(row["disk_read_bytes"]) for row in rows) if disk_ok else None
    write_bytes = sum(int(row["disk_write_bytes"]) for row in rows) if disk_ok else None
    return {
        "schema": "tool_resource_observation_v1",
        "backend": "ebpf",
        "fallback_used": False,
        "scope": "process_tree",
        "attribution": "exclusive_process_tree",
        "window": {
            "clock": clock,
            "kind": "execution",
            "requested_start_ns": str(started_ns),
            "requested_end_ns": str(ended_ns),
            "observed_start_ns": str(observed_start) if observed_start is not None else None,
            "observed_end_ns": str(observed_end) if observed_end is not None else None,
            "coverage_ratio": covered / (ended_ns - started_ns) if comparable and ended_ns > started_ns else None,
            "complete": complete,
        },
        "metrics": {
            "cpu_time": {
                "available": cpu_ok,
                "eligible": cpu_ok and complete,
                "reason": ("ok" if complete else "execution_window_only") if cpu_ok else "clause_cpu_unavailable",
                "measurement": "ebpf_owned_lineage_cpu_time",
                "value_seconds": cpu_seconds,
                "average_cores": None if cpu_seconds is None or not complete or duration_s <= 0 else cpu_seconds / duration_s,
                "sample_count": None,
            },
            "cpu_peak": {
                "available": peak_cpu_ok,
                "eligible": peak_cpu_ok and complete,
                "reason": ("ok" if complete else "execution_window_only") if peak_cpu_ok else ("aligned_call_profile_unavailable" if len(rows) > 1 else "clause_cpu_peak_unavailable"),
                "measurement": "ebpf_owned_lineage_cpu_500ms_peak",
                "value_cores": float(peak_cpu) if peak_cpu_ok else None,
                "window_ms": 500,
            },
            "memory_peak": {
                "available": memory_ok,
                "eligible": memory_ok and complete,
                "reason": ("ok" if complete else "execution_window_only") if memory_ok else ("aligned_call_profile_unavailable" if len(rows) > 1 else "clause_memory_unavailable"),
                "measurement": "ebpf_sampled_distinct_mm_rss",
                "value_bytes": int(float(peak_memory_mb) * 1_000_000) if memory_ok else None,
                "sample_count": None,
                "counter_exact": False,
            },
            "disk_io": {
                "available": disk_ok,
                "eligible": disk_ok and complete,
                "reason": ("ok" if complete else "execution_window_only") if disk_ok else "clause_disk_io_unavailable",
                "measurement": "ebpf_task_io_accounting",
                "read_bytes": read_bytes,
                "write_bytes": write_bytes,
            },
            "network_io": _unavailable_metric("ebpf_network_unavailable", measurement="ebpf_tcp_send_recv_bytes"),
        },
        "execution_telemetry_quality": call.get("telemetry_quality"),
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

