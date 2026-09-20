"""Environment memory labels. Never reinterpret process RSS as VM memory."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from pathlib import Path
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
            or measurement not in {"cgroup_v2_memory_current", "guest_memtotal_minus_memavailable"}
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
        "cgroup_v2_memory_current", "guest_memtotal_minus_memavailable"
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
        self._recent: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _read(path: str) -> int | None:
        try:
            value = int((Path(path) / "memory.current").read_text().strip())
            return value if value >= 0 else None
        except (OSError, ValueError):
            return None

    def begin(self, key: Any, scope: Any) -> None:
        path = getattr(scope, "cgroup_path", None)
        if not path or Path(path).as_posix().rstrip("/") in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}:
            return
        # Host service cgroups (e.g. sshd.service) are not task environments.
        if not (getattr(scope, "container_id", None) or
                getattr(scope, "attribution_source", None) == "exclusive-execution-cgroup"):
            with self._lock:
                self._unavailable[key] = "unverified_task_environment"
            return
        value = self._read(path)
        if value is None:
            return
        with self._lock:
            if key in self._active:
                return
            now = time.time()
            identity = (path, str(getattr(scope, "container_id", None) or "exclusive-execution-cgroup"))
            recent = self._recent.get(identity)
            # Freshness is relative to the actual start, known at completion,
            # not the possibly delayed arrival of this begin request.
            points = list(recent["points"]) if recent else []
            points.append((now, value))
            if identity not in self._recent and len(self._recent) >= 128:
                self._recent.pop(min(self._recent, key=lambda k: self._recent[k]["last_used"]))
            self._recent[identity] = {
                "points": deque(points, maxlen=16), "last_used": now,
                "previous_end": recent["previous_end"] if recent else None,
            }
            self._unavailable.pop(key, None)
            overlap = [row for row in self._active.values() if Path(row["path"]).is_relative_to(Path(path)) or Path(path).is_relative_to(Path(row["path"]))]
            for row in overlap:
                row["exclusive"] = False
            self._active[key] = {"path": path, "baseline": value,
                                 "identity": identity,
                                 "previous_end": recent["previous_end"] if recent else None,
                                 "points": deque(points, maxlen=MAX_MEMORY_TIMELINE_POINTS),
                                 "timeline_truncated": False, "peak": value, "polls": 0,
                                 "exclusive": not overlap}

    def poll(self) -> None:
        with self._lock:
            now = time.time()
            values = {}
            for identity, recent in list(self._recent.items()):
                if now - recent["last_used"] > 60:
                    self._recent.pop(identity)
                    continue
                value = self._read(identity[0])
                sampled_at = time.time()
                values[identity] = (sampled_at, value)
                if value is not None:
                    recent["points"].append((sampled_at, value))
            for row in self._active.values():
                sampled_at, value = values.get(row["identity"], (now, None))
                if row["identity"] in self._recent:
                    self._recent[row["identity"]]["last_used"] = now
                else:
                    value = self._read(row["path"])
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
            if row and row["identity"] in self._recent:
                recent = self._recent[row["identity"]]
                recent["previous_end"] = max(recent["previous_end"] or 0, ended_at or time.time())
                recent["last_used"] = time.time()
        if reason:
            return {"memory_eligible": False, "memory_unavailable_reason": reason}
        if row is None:
            return None
        if row.get("timeline_truncated"):
            return {"memory_eligible": False, "memory_unavailable_reason": "memory_timeline_truncated"}
        # Completion processing can run seconds after the payload finished.
        # Never use a fresh completion-time read as an execution peak.
        points = list(row["points"])
        reason = None
        if (started_at is None or ended_at is None
                or not math.isfinite(started_at) or not math.isfinite(ended_at)
                or ended_at <= started_at):
            reason = "execution_window_unavailable"
        elif not row["exclusive"] or (row["previous_end"] is not None and row["previous_end"] > started_at):
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
            return {"memory_eligible": False, "memory_unavailable_reason": reason}
        result = environment_memory(
            baseline=before[-1][1], values=[p[1] for p in inside],
            environment_id=row["path"], measurement="cgroup_v2_memory_current",
            baseline_before_start=True, exclusive=row["exclusive"],
        )
        if result is None:
            return {"memory_eligible": False, "memory_unavailable_reason": "overlapping_environment_calls"}
        return {**result.fields(), "memory_timeline": [before[-1], *inside]}

    def discard(self, key: Any) -> None:
        with self._lock:
            row = self._active.pop(key, None)
            if row:
                # An abandoned call has no trustworthy end for overlap checks.
                self._recent.pop(row["identity"], None)
            self._unavailable.pop(key, None)
