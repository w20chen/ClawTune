"""Environment memory labels. Never reinterpret process RSS as VM memory."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from pathlib import Path, PurePosixPath
import threading
import time
from collections import deque


MAX_MEMORY_TIMELINE_POINTS = 20_000


@dataclass(frozen=True)
class EnvironmentMemory:
    baseline_bytes: int
    total_peak_bytes: int
    extra_peak_bytes: int
    environment_id: str
    measurement: str

    def fields(self) -> dict[str, Any]:
        return {
            "memory_baseline_bytes": self.baseline_bytes,
            "memory_total_peak_bytes": self.total_peak_bytes,
            "memory_extra_peak_bytes": self.extra_peak_bytes,
            "memory_environment_id": self.environment_id,
            "memory_measurement": self.measurement,
            "memory_eligible": True,
        }


def environment_memory(*, baseline: int | None, values: Sequence[int],
                       environment_id: str | None, measurement: str,
                       baseline_before_start: bool, exclusive: bool) -> EnvironmentMemory | None:
    """Only an unchanged, exclusive environment with a pre-start baseline qualifies.

    Sources intentionally remain separate: cgroup memory.current includes cache;
    a VM guest adapter must declare its own measurement, never use host VM RSS.
    """
    if (not exclusive or not baseline_before_start or not environment_id
            or measurement not in {"cgroup_v2_memory_current", "cgroup_v2_environment_union_v1", "guest_memtotal_minus_memavailable"}
            or baseline is None or isinstance(baseline, bool) or not isinstance(baseline, (int, float)) or baseline < 0 or not values):
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in [baseline, *values]):
        return None
    peak = int(max(values))
    return EnvironmentMemory(int(baseline), peak, max(0, peak - baseline), environment_id, measurement)


def memory_labels(record: Mapping[str, Any]) -> dict[str, float]:
    """Validate paired labels at KB ingress; partial/mixed-source rows stay absent."""
    if not record.get("memory_environment_id") or record.get("memory_eligible") is not True or record.get("memory_measurement") not in {
        "cgroup_v2_memory_current", "cgroup_v2_environment_union_v1", "guest_memtotal_minus_memavailable"
    }:
        return {}
    keys = ("memory_baseline_bytes", "memory_total_peak_bytes", "memory_extra_peak_bytes")
    vals = [record.get(k) for k in keys]
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in vals):
        return {}
    baseline, total, extra = vals
    if not math.isclose(extra, max(0, total - baseline), abs_tol=1):
        return {}
    return {keys[1]: float(total), keys[2]: float(extra)}


def clause_memory_labels(environment: Mapping[str, Any], clauses: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Use environment samples only for nonoverlapping clause intervals.

    Includes *all* clauses when checking overlap, even excluded pipe viewers.
    Never distribute a shared environment peak across concurrent children.
    """
    if not memory_labels(environment):
        return [{} for _ in clauses]
    points = environment.get("memory_timeline") or []
    results = []
    for i, clause in enumerate(clauses):
        start, end = clause.get("ts_start"), clause.get("ts_end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
            results.append({})
            continue
        overlap = any(j != i and other.get("ts_start", end) < end
                      and other.get("ts_end", start) > start for j, other in enumerate(clauses))
        before = [(t, v) for t, v in points if t <= start]
        inside_points = [(t, v) for t, v in points if start < t <= end]
        inside = [v for _, v in inside_points]
        if not before or start - before[-1][0] > .15 or not inside:
            results.append({})
            continue
        window = [start, *[t for t, _ in inside_points], end]
        if any(b < a or b - a > .15 for a, b in zip(window, window[1:])):
            results.append({})
            continue
        label = environment_memory(baseline=before[-1][1], values=inside,
            environment_id=environment.get("memory_environment_id"),
            measurement=environment.get("memory_measurement", ""),
            baseline_before_start=True, exclusive=not overlap)
        results.append(label.fields() if label else {})
    return results


class EnvironmentMemoryMonitor:
    """Independent task-cgroup timeline; PID scope rebases never reset its baseline."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[Any, dict[str, Any]] = {}
        self._unavailable: dict[Any, str] = {}
        self._recent: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
        self._completed_windows: deque[dict[str, Any]] = deque(maxlen=512)

    @staticmethod
    def _read(path: str) -> int | None:
        try:
            value = int((Path(path) / "memory.current").read_text().strip())
            return value if value >= 0 else None
        except (OSError, ValueError):
            return None

    @classmethod
    def _read_paths(cls, paths: Sequence[str]) -> int | None:
        values = [cls._read(path) for path in paths]
        if not values or any(value is None for value in values):
            return None
        return sum(int(value) for value in values if value is not None)

    @staticmethod
    def _nonoverlapping_paths(paths: Sequence[str]) -> tuple[str, ...]:
        normalized = sorted({str(PurePosixPath(path.replace("\\", "/"))) for path in paths if path})
        kept: list[str] = []
        for path in normalized:
            candidate = PurePosixPath(path)
            if any(
                candidate == PurePosixPath(parent)
                or candidate.is_relative_to(PurePosixPath(parent))
                for parent in kept
            ):
                continue
            kept = [
                parent for parent in kept
                if not PurePosixPath(parent).is_relative_to(candidate)
            ]
            kept.append(path)
        return tuple(sorted(kept))

    @staticmethod
    def _paths_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
        return any(
            PurePosixPath(a) == PurePosixPath(b)
            or PurePosixPath(a).is_relative_to(PurePosixPath(b))
            or PurePosixPath(b).is_relative_to(PurePosixPath(a))
            for a in left
            for b in right
        )

    def _activate(
        self, key: Any, paths: Sequence[str], environment_id: str,
        measurement: str = "cgroup_v2_memory_current",
    ) -> None:
        paths = self._nonoverlapping_paths(paths)
        value = self._read_paths(paths)
        if not paths or value is None:
            with self._lock:
                self._active.pop(key, None)
                self._unavailable[key] = "environment_memory_scope_unreadable"
            return
        with self._lock:
            now = time.time()
            identity = (paths, environment_id)
            recent = self._recent.get(identity)
            points = list(recent["points"]) if recent else []
            points.append((now, value))
            if identity not in self._recent and len(self._recent) >= 128:
                self._recent.pop(
                    min(self._recent, key=lambda item: self._recent[item]["last_used"])
                )
            self._recent[identity] = {
                "points": deque(points, maxlen=16),
                "last_used": now,
                "previous_end": recent["previous_end"] if recent else None,
            }
            self._unavailable.pop(key, None)
            self._active[key] = {
                "paths": paths,
                "path": environment_id,
                "measurement": measurement,
                "baseline": value,
                "identity": identity,
                "previous_end": recent["previous_end"] if recent else None,
                "points": deque(points, maxlen=MAX_MEMORY_TIMELINE_POINTS),
                "timeline_truncated": False,
                "peak": value,
                "polls": 0,
                "activated_at": now,
                "exclusive": True,
            }

    def begin(self, key: Any, scope: Any) -> None:
        path = getattr(scope, "cgroup_path", None)
        if not path or Path(path).as_posix().rstrip("/") in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}:
            return
        with self._lock:
            active = self._active.get(key)
            if active is not None:
                # A process-scope refinement is not a new environment. Keep
                # the pre-action baseline, but reject a different task scope.
                same_path = any(PurePosixPath(path).is_relative_to(PurePosixPath(p))
                                for p in active["paths"])
                container = getattr(scope, "container_id", None)
                if same_path and (not container or container == active["path"]):
                    return
                self._active.pop(key, None)
                self._unavailable[key] = "task_environment_changed"
                return
        # Host service cgroups (e.g. sshd.service) are not task environments.
        if not (getattr(scope, "container_id", None) or
                getattr(scope, "attribution_source", None) in {
                    "exclusive-execution-cgroup", "exclusive-task-cgroup"}):
            with self._lock:
                self._unavailable[key] = "unverified_task_environment"
            return
        with self._lock:
            if key in self._active:
                return
        self._activate(
            key,
            (path,),
            str(getattr(scope, "container_id", None) or path),
        )

    def bind_environment(
        self,
        key: Any,
        *,
        base_scope: Any | None,
        execution_parent_path: str,
        environment_id: str,
    ) -> None:
        """Measure charges retained before and after execution migration."""
        paths = [execution_parent_path]
        base_path = getattr(base_scope, "cgroup_path", None)
        if base_path:
            paths.insert(0, base_path)
        self._activate(
            key,
            paths,
            environment_id,
            measurement="cgroup_v2_environment_union_v1",
        )

    def poll(self) -> None:
        with self._lock:
            now = time.time()
            values = {}
            for identity, recent in list(self._recent.items()):
                if now - recent["last_used"] > 60:
                    self._recent.pop(identity)
                    continue
                value = self._read_paths(identity[0])
                sampled_at = time.time()
                values[identity] = (sampled_at, value)
                if value is not None:
                    recent["points"].append((sampled_at, value))
            for row in self._active.values():
                sampled_at, value = values.get(row["identity"], (now, None))
                if row["identity"] in self._recent:
                    self._recent[row["identity"]]["last_used"] = now
                else:
                    value = self._read_paths(row["paths"])
                    sampled_at = time.time()
                if value is not None:
                    row["polls"] += 1
                    if len(row["points"]) == row["points"].maxlen:
                        row["timeline_truncated"] = True
                    row["points"].append((sampled_at, value))
                    row["peak"] = max(row["peak"], value)

    def complete(self, key: Any, *, started_at: float | None = None,
                 ended_at: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            row = self._active.pop(key, None)
            reason = self._unavailable.pop(key, None)
            valid_window = (
                row is not None
                and isinstance(started_at, (int, float))
                and not isinstance(started_at, bool)
                and isinstance(ended_at, (int, float))
                and not isinstance(ended_at, bool)
                and math.isfinite(started_at)
                and math.isfinite(ended_at)
                and ended_at > started_at
            )
            overlaps_active = bool(
                valid_window
                and any(
                    self._paths_overlap(row["paths"], peer["paths"])
                    and peer["activated_at"] < ended_at
                    for peer in self._active.values()
                )
            )
            overlaps_completed = bool(
                valid_window
                and any(
                    self._paths_overlap(row["paths"], prior["paths"])
                    and prior["started_at"] < ended_at
                    and prior["ended_at"] > started_at
                    for prior in self._completed_windows
                )
            )
            if row is not None:
                row["exclusive"] = not (overlaps_active or overlaps_completed)
            if valid_window:
                self._completed_windows.append(
                    {
                        "paths": row["paths"],
                        "started_at": started_at,
                        "ended_at": ended_at,
                    }
                )
            if row and row["identity"] in self._recent:
                recent = self._recent[row["identity"]]
                recent["previous_end"] = max(recent["previous_end"] or 0, ended_at or time.time())
                recent["last_used"] = time.time()
        if reason:
            return {"memory_eligible": False, "memory_unavailable_reason": reason}
        if row is None:
            return None
        points = list(row["points"])
        diagnostics = self._diagnostics(row, points, started_at, ended_at)
        if row.get("timeline_truncated"):
            return {
                "memory_eligible": False,
                "memory_unavailable_reason": "memory_timeline_truncated",
                "memory_diagnostics": diagnostics,
            }
        # Completion processing can run seconds after the payload finished.
        # Never use a fresh completion-time read as an execution peak.
        reason = None
        if (started_at is None or ended_at is None
                or not math.isfinite(started_at) or not math.isfinite(ended_at)
                or ended_at <= started_at):
            reason = "execution_window_unavailable"
        elif not row["exclusive"]:
            reason = "overlapping_environment_calls"
        elif points[0][0] > started_at:
            reason = "baseline_after_execution_start"
        elif started_at - points[0][0] > .15:
            # Use the latest pre-start sample below, if it is fresh enough.
            before = [p for p in points if p[0] <= started_at]
            if not before or started_at - before[-1][0] > .15:
                reason = "stale_memory_baseline"
        before = [p for p in points if started_at is not None and p[0] <= started_at]
        inside = [p for p in points if started_at is not None and ended_at is not None
                  and started_at < p[0] <= ended_at]
        if reason is None:
            if not inside:
                reason = "no_in_execution_memory_sample"
            else:
                window = [started_at, *[p[0] for p in inside], ended_at]
                if any(b < a or b - a > .15 for a, b in zip(window, window[1:])):
                    reason = "memory_sampling_gap"
        if reason:
            return {
                "memory_eligible": False,
                "memory_unavailable_reason": reason,
                "memory_diagnostics": diagnostics,
            }
        result = environment_memory(
            baseline=before[-1][1], values=[p[1] for p in inside],
            environment_id=row["path"], measurement=row["measurement"],
            baseline_before_start=True, exclusive=row["exclusive"],
        )
        if result is None:
            return {
                "memory_eligible": False,
                "memory_unavailable_reason": "overlapping_environment_calls",
                "memory_diagnostics": diagnostics,
            }
        return {**result.fields(), "memory_timeline": [before[-1], *inside]}

    @staticmethod
    def _diagnostics(
        row: Mapping[str, Any],
        points: Sequence[tuple[float, int]],
        started_at: float | None,
        ended_at: float | None,
    ) -> dict[str, Any]:
        valid_window = (
            isinstance(started_at, (int, float))
            and not isinstance(started_at, bool)
            and isinstance(ended_at, (int, float))
            and not isinstance(ended_at, bool)
            and math.isfinite(started_at)
            and math.isfinite(ended_at)
            and ended_at > started_at
        )
        before = [point for point in points if valid_window and point[0] <= started_at]
        inside = [
            point for point in points
            if valid_window and started_at < point[0] <= ended_at
        ]
        baseline = before[-1] if before else None
        total = max((value for _, value in inside), default=None)
        extra = (
            max(0, total - baseline[1])
            if baseline is not None and total is not None
            else None
        )
        return {
            "memory_environment_id": row["path"],
            "memory_measurement": row["measurement"],
            "window_start_s": started_at if valid_window else None,
            "window_end_s": ended_at if valid_window else None,
            "baseline_sample": list(baseline) if baseline is not None else None,
            "observed_baseline_bytes": baseline[1] if baseline is not None else None,
            "observed_total_peak_bytes": total,
            "observed_extra_peak_bytes": extra,
            "samples": [
                list(point)
                for point in [*([baseline] if baseline is not None else []), *inside]
            ],
            "exclusive": bool(row["exclusive"]),
            "timeline_truncated": bool(row.get("timeline_truncated")),
        }

    def discard(self, key: Any) -> None:
        with self._lock:
            row = self._active.pop(key, None)
            if row:
                # An abandoned call has no trustworthy end for overlap checks.
                self._recent.pop(row["identity"], None)
            self._unavailable.pop(key, None)
