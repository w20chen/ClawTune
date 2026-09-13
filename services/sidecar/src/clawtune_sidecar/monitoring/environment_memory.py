"""Environment memory labels. Never reinterpret process RSS as VM memory."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from pathlib import Path
import threading
import time
from collections import deque


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
        inside = [v for t, v in points if start <= t <= end]
        if not before or start - before[-1][0] > .15 or not inside:
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
        value = self._read(path)
        if value is None:
            return
        with self._lock:
            if key in self._active:
                return
            overlap = [row for row in self._active.values() if Path(row["path"]).is_relative_to(Path(path)) or Path(path).is_relative_to(Path(row["path"]))]
            for row in overlap:
                row["exclusive"] = False
            self._active[key] = {"path": path, "baseline": value,
                                 "points": deque([(time.time(), value)], maxlen=20000), "peak": value, "polls": 0, "exclusive": not overlap}

    def poll(self) -> None:
        with self._lock:
            for row in self._active.values():
                value = self._read(row["path"])
                if value is not None:
                    row["polls"] += 1
                    row["points"].append((time.time(), value))
                    row["peak"] = max(row["peak"], value)

    def complete(self, key: Any) -> dict[str, Any] | None:
        with self._lock:
            row = self._active.pop(key, None)
        if row is None:
            return None
        value = self._read(row["path"])
        if value is not None:
            row["points"].append((time.time(), value))
            row["peak"] = max(row["peak"], value)
        if row["polls"] == 0:
            return {"memory_eligible": False, "memory_unavailable_reason": "no_in_execution_memory_sample"}
        result = environment_memory(
            baseline=row["baseline"], values=[row["peak"]],
            environment_id=row["path"], measurement="cgroup_v2_memory_current",
            baseline_before_start=True, exclusive=row["exclusive"],
        )
        if result is None:
            return {"memory_eligible": False, "memory_unavailable_reason": "overlapping_environment_calls"}
        return {**result.fields(), "memory_timeline": list(row["points"])}

    def discard(self, key: Any) -> None:
        with self._lock:
            self._active.pop(key, None)
