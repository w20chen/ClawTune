from __future__ import annotations

import math
import os
import platform
import re
from pathlib import Path
from typing import Any


_CPU_SYSFS_ROOT = Path("/sys/devices/system/cpu")
_NODE_SYSFS_ROOT = Path("/sys/devices/system/node")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")


def read_topology(
    *,
    reserve_ratio: float = 0.05,
    reserve_cores: int | None = None,
    cpu_budget_cores: float | None = None,
) -> dict[str, Any]:
    """Return host topology plus a conservative, machine-sized CPU budget.

    ``os.cpu_count()`` describes the host and can overstate what a sidecar is
    allowed to use.  Capacity is therefore based on the intersection of
    online CPUs, the process affinity mask, and cgroup-v2's effective cpuset,
    then capped by the tightest ``cpu.max`` quota in the current cgroup chain.
    Placement remains advisory; this function never changes affinity/cgroups.
    """

    _validate_capacity_options(reserve_ratio, reserve_cores, cpu_budget_cores)
    if os.name != "posix":
        return {
            "available": False,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "online_cpus": [],
            "effective_cpus": [],
            "affinity_cpus": None,
            "cpuset_effective_cpus": None,
            "cpu_quota_cores": None,
            "cpu_capacity_cores": 0.0,
            "cpu_reserve_ratio": reserve_ratio,
            "reserved_cpu_cores": 0,
            "cpu_budget_limit_cores": cpu_budget_cores,
            "tool_cpu_budget_cores": 0.0,
            "numa_nodes": [],
            "llc_clusters": [],
            "reason": "procfs/sysfs topology is only available on Linux-like systems",
        }

    online = _read_cpu_list(_CPU_SYSFS_ROOT / "online")
    affinity = _read_affinity_cpus()
    cpuset = _read_effective_cpuset()
    effective = set(online)
    if affinity is not None:
        effective.intersection_update(affinity)
    if cpuset is not None:
        effective.intersection_update(cpuset)

    quota = _read_effective_cpu_quota()
    detected_capacity = float(len(effective))
    if quota is not None:
        detected_capacity = min(detected_capacity, quota)
    detected_capacity = max(0.0, detected_capacity)
    reserved = _reserved_cores(
        detected_capacity,
        reserve_ratio=reserve_ratio,
        explicit=reserve_cores,
    )
    tool_budget = max(0.0, detected_capacity - reserved)
    if cpu_budget_cores is not None:
        tool_budget = min(tool_budget, cpu_budget_cores)

    return {
        "available": True,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "online_cpus": online,
        "effective_cpus": sorted(effective),
        "affinity_cpus": None if affinity is None else sorted(affinity),
        "cpuset_effective_cpus": None if cpuset is None else sorted(cpuset),
        "cpu_quota_cores": _rounded_capacity(quota),
        "cpu_capacity_cores": _rounded_capacity(detected_capacity),
        "cpu_reserve_ratio": reserve_ratio,
        "reserved_cpu_cores": reserved,
        "cpu_budget_limit_cores": cpu_budget_cores,
        "tool_cpu_budget_cores": _rounded_capacity(tool_budget),
        "numa_nodes": _read_numa_nodes(effective),
        "llc_clusters": _read_llc_clusters(effective),
    }


def _validate_capacity_options(
    reserve_ratio: float,
    reserve_cores: int | None,
    cpu_budget_cores: float | None,
) -> None:
    if not math.isfinite(reserve_ratio) or not 0.0 <= reserve_ratio <= 1.0:
        raise ValueError("reserve_ratio must be between 0 and 1")
    if reserve_cores is not None and reserve_cores < 0:
        raise ValueError("reserve_cores must be non-negative")
    if cpu_budget_cores is not None and (
        not math.isfinite(cpu_budget_cores) or cpu_budget_cores < 0.0
    ):
        raise ValueError("cpu_budget_cores must be non-negative")


def _reserved_cores(
    capacity: float,
    *,
    reserve_ratio: float,
    explicit: int | None,
) -> int:
    # Always leave at least one whole logical CPU of detected capacity for
    # tool work.  A one-CPU or sub-two-CPU cgroup therefore reserves zero.
    max_safe_reserve = max(0, math.floor(capacity) - 1)
    requested = explicit if explicit is not None else math.ceil(capacity * reserve_ratio)
    return min(requested, max_safe_reserve)


def _rounded_capacity(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_cpu_list(path: Path) -> list[int]:
    parsed = _parse_cpu_list(_read_text(path))
    return sorted(parsed) if parsed is not None else list(range(os.cpu_count() or 0))


def _parse_cpu_list(text: str | None) -> set[int] | None:
    if text is None or not text.strip():
        return None
    cpus: set[int] = set()
    try:
        for raw_part in text.split(","):
            part = raw_part.strip()
            if not part:
                return None
            if "-" in part:
                start_raw, end_raw = part.split("-", 1)
                start = int(start_raw)
                end = int(end_raw)
                if start < 0 or end < start:
                    return None
                cpus.update(range(start, end + 1))
            else:
                cpu = int(part)
                if cpu < 0:
                    return None
                cpus.add(cpu)
    except ValueError:
        return None
    return cpus


def _format_cpu_list(cpus: set[int]) -> str:
    if not cpus:
        return ""
    ordered = sorted(cpus)
    ranges: list[str] = []
    start = previous = ordered[0]
    for cpu in ordered[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = cpu
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _read_affinity_cpus() -> set[int] | None:
    get_affinity = getattr(os, "sched_getaffinity", None)
    if not callable(get_affinity):
        return None
    try:
        return {int(cpu) for cpu in get_affinity(0) if int(cpu) >= 0}
    except (OSError, TypeError, ValueError):
        return None


def _current_cgroup_dir() -> Path | None:
    raw = _read_text(_PROC_SELF_CGROUP)
    if raw is None:
        return _CGROUP_ROOT if _CGROUP_ROOT.is_dir() else None
    relative: str | None = None
    for line in raw.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            relative = fields[2]
            break
    if relative is None:
        return _CGROUP_ROOT if _CGROUP_ROOT.is_dir() else None
    root = _CGROUP_ROOT.resolve()
    candidate = (root / relative.lstrip("/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else (root if root.is_dir() else None)


def _cgroup_ancestors() -> list[Path]:
    current = _current_cgroup_dir()
    if current is None:
        return []
    root = _CGROUP_ROOT.resolve()
    ancestors: list[Path] = []
    candidate = current
    while True:
        ancestors.append(candidate)
        if candidate == root:
            break
        parent = candidate.parent
        if parent == candidate:
            break
        try:
            parent.relative_to(root)
        except ValueError:
            break
        candidate = parent
    return ancestors


def _read_effective_cpuset() -> set[int] | None:
    for cgroup in _cgroup_ancestors():
        value = _parse_cpu_list(_read_text(cgroup / "cpuset.cpus.effective"))
        if value is not None:
            return value
    return None


def _read_effective_cpu_quota() -> float | None:
    quotas: list[float] = []
    for cgroup in _cgroup_ancestors():
        raw = _read_text(cgroup / "cpu.max")
        if raw is None:
            continue
        fields = raw.split()
        if len(fields) != 2 or fields[0] == "max":
            continue
        try:
            quota = int(fields[0])
            period = int(fields[1])
        except ValueError:
            continue
        if quota > 0 and period > 0:
            quotas.append(quota / period)
    return min(quotas) if quotas else None


def _read_numa_nodes(effective_cpus: set[int]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if not _NODE_SYSFS_ROOT.exists():
        return nodes
    for node in sorted(_NODE_SYSFS_ROOT.glob("node*")):
        match = re.fullmatch(r"node(\d+)", node.name)
        if match is None or not node.is_dir():
            continue
        parsed = _parse_cpu_list(_read_text(node / "cpulist"))
        cpus = set() if parsed is None else parsed.intersection(effective_cpus)
        nodes.append(
            {
                "node": int(match.group(1)),
                "cpulist": _format_cpu_list(cpus),
                "cpus": sorted(cpus),
            }
        )
    return nodes


def _read_llc_clusters(effective_cpus: set[int]) -> list[dict[str, Any]]:
    """Select each CPU's highest-level Data/Unified cache as its LLC."""

    highest_by_cpu: dict[int, tuple[int, str, set[int]]] = {}
    for cache in _CPU_SYSFS_ROOT.glob("cpu*/cache/index*"):
        cpu_match = re.fullmatch(r"cpu(\d+)", cache.parents[1].name)
        if cpu_match is None:
            continue
        cpu = int(cpu_match.group(1))
        if cpu not in effective_cpus:
            continue
        cache_type = (_read_text(cache / "type") or "").lower()
        if cache_type not in {"data", "unified"}:
            continue
        try:
            level = int(_read_text(cache / "level") or "")
        except ValueError:
            continue
        shared = _parse_cpu_list(_read_text(cache / "shared_cpu_list"))
        if level <= 0 or shared is None:
            continue
        shared.intersection_update(effective_cpus)
        if not shared:
            continue
        normalized_type = "Unified" if cache_type == "unified" else "Data"
        previous = highest_by_cpu.get(cpu)
        if previous is None or (level, normalized_type == "Unified") > (
            previous[0],
            previous[1] == "Unified",
        ):
            highest_by_cpu[cpu] = (level, normalized_type, shared)

    clusters: dict[tuple[int, str, str], dict[str, Any]] = {}
    for level, cache_type, shared in highest_by_cpu.values():
        cpulist = _format_cpu_list(shared)
        clusters[(level, cache_type, cpulist)] = {
            "level": level,
            "cache_type": cache_type,
            "shared_cpu_list": cpulist,
            "cpus": sorted(shared),
        }
    return [clusters[key] for key in sorted(clusters)]
