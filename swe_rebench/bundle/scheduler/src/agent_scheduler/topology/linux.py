from __future__ import annotations

import math
import os
import platform
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CPU_SYSFS_ROOT = Path("/sys/devices/system/cpu")
_NODE_SYSFS_ROOT = Path("/sys/devices/system/node")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
_PROC_STAT = Path("/proc/stat")


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


@dataclass(frozen=True)
class CpuTicks:
    """Per-CPU tick counters from ``/proc/stat`` (USER_HZ jiffies).

    Fields follow the ``cpu<N>`` line layout: user, nice, system, idle,
    iowait, irq, softirq, steal, guest, guest_nice.  Missing trailing
    counters (older kernels) default to zero.
    """

    user: int = 0
    nice: int = 0
    system: int = 0
    idle: int = 0
    iowait: int = 0
    irq: int = 0
    softirq: int = 0
    steal: int = 0
    guest: int = 0
    guest_nice: int = 0

    def total(self) -> int:
        """All ticks attributed to this CPU, including idle and iowait."""
        return (
            self.user
            + self.nice
            + self.system
            + self.idle
            + self.iowait
            + self.irq
            + self.softirq
            + self.steal
        )

    def busy(self) -> int:
        """Ticks spent doing work.

        guest/guest_nice are already folded into user/nice by the kernel, so
        only idle and iowait are subtracted.
        """
        return self.total() - self.idle - self.iowait


def _user_hz() -> int:
    """Return the kernel USER_HZ clock tick rate used by ``/proc/stat``."""
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return 100


def _read_proc_stat_ticks() -> dict[int, CpuTicks]:
    """Read per-CPU tick counters from ``/proc/stat``.

    The aggregate ``cpu`` line is skipped; only ``cpu<N>`` lines are kept.
    On non-Linux hosts (or read failures) an empty mapping is returned so
    callers can report the sample as unavailable instead of raising.
    """
    ticks: dict[int, CpuTicks] = {}
    try:
        text = _PROC_STAT.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ticks
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or not fields[0].startswith("cpu"):
            continue
        cpu_field = fields[0]
        if cpu_field == "cpu":
            continue
        cpu_part = cpu_field[3:]
        if not cpu_part.isdigit():
            continue
        try:
            values = [int(value) for value in fields[1:11]]
        except ValueError:
            continue
        if len(values) < 10:
            values.extend([0] * (10 - len(values)))
        ticks[int(cpu_part)] = CpuTicks(*values)
    return ticks


def _host_numa_nodes() -> list[dict[str, Any]]:
    """Read all host NUMA nodes without intersecting the sidecar's cpuset.

    NUMA-domain busyness is a host-level property: the sidecar's own affinity
    mask must not hide CPUs that other tenants are using on the same node, so
    ``sample()`` deliberately reports every node's full CPU membership.
    """
    nodes: list[dict[str, Any]] = []
    if not _NODE_SYSFS_ROOT.exists():
        return nodes
    for node in sorted(_NODE_SYSFS_ROOT.glob("node*")):
        match = re.fullmatch(r"node(\d+)", node.name)
        if match is None or not node.is_dir():
            continue
        parsed = _parse_cpu_list(_read_text(node / "cpulist"))
        cpus = [] if parsed is None else sorted(parsed)
        nodes.append(
            {
                "node": int(match.group(1)),
                "cpulist": _format_cpu_list(set(cpus)),
                "cpus": cpus,
            }
        )
    return nodes


def _aggregate_node_delta(
    cpus: Sequence[int],
    prev_ticks: Mapping[int, CpuTicks],
    ticks: Mapping[int, CpuTicks],
) -> tuple[int, int, int] | None:
    """Aggregate ``(busy_delta, total_delta, covered_cpus)`` across *cpus*.

    Only CPUs present in both tick maps are counted; ``covered_cpus`` is the
    number that actually contributed (i.e. the online count observed in the
    window).  Returns ``None`` when no CPU contributed or the window saw no
    ticks at all.
    """
    busy = 0
    total = 0
    covered = 0
    for cpu in cpus:
        before = prev_ticks.get(cpu)
        after = ticks.get(cpu)
        if before is None or after is None:
            continue
        covered += 1
        busy += max(0, after.busy() - before.busy())
        total += max(0, after.total() - before.total())
    if covered == 0 or total <= 0:
        return None
    return busy, total, covered


def _numa_node_usage_payload(
    node: Mapping[str, Any],
    *,
    online_cpus: int,
    busy_cores: float | None,
    utilization_pct: float | None,
) -> dict[str, Any]:
    return {
        "node": int(node["node"]),
        "cpulist": node.get("cpulist"),
        "online_cpus": online_cpus,
        "cpu_utilization_pct": utilization_pct,
        "busy_cores": busy_cores,
    }


class NumaCpuUsageSampler:
    """Delta-based per-NUMA-node CPU utilization sampler.

    Every :meth:`sample` reads per-CPU tick counters from ``/proc/stat`` and
    aggregates the delta since the previous sample by each NUMA node's CPU
    membership from sysfs.  The per-node ``cpu_utilization_pct`` is the total
    busy share of the whole NUMA domain (0-100%, where 100% means every CPU in
    the node was fully busy); ``busy_cores`` is the average number of fully
    busy cores over the window.

    The baseline is seeded at construction so the first prediction still
    reports a window (from sidecar start).  Samples are cheap (one short
    procfs read) and guarded by a lock because predictions can run on
    concurrent worker threads.  One data point is emitted per hardware NUMA
    node, so a 4-node machine yields exactly four entries.
    """

    def __init__(self, numa_nodes: Sequence[dict[str, Any]] | None = None) -> None:
        self._nodes = list(numa_nodes) if numa_nodes is not None else _host_numa_nodes()
        self._user_hz = _user_hz()
        self._lock = threading.Lock()
        self._prev_ticks: dict[int, CpuTicks] | None = _read_proc_stat_ticks()
        self._prev_monotonic_s: float | None = time.monotonic()

    def sample(self) -> dict[str, Any]:
        with self._lock:
            ticks = _read_proc_stat_ticks()
            now = time.monotonic()
            prev_ticks = self._prev_ticks
            prev_monotonic = self._prev_monotonic_s
            self._prev_ticks = ticks
            self._prev_monotonic_s = now

            if prev_ticks is None or not ticks or not self._nodes:
                return {
                    "available": bool(ticks) and bool(self._nodes),
                    "sampled": False,
                    "node_count": len(self._nodes),
                    "window_s": None,
                    "user_hz": self._user_hz,
                    "nodes": [
                        _numa_node_usage_payload(
                            node,
                            online_cpus=len(node.get("cpus", [])),
                            busy_cores=None,
                            utilization_pct=None,
                        )
                        for node in self._nodes
                    ],
                }

            window_s = max(0.0, now - prev_monotonic)
            nodes: list[dict[str, Any]] = []
            for node in self._nodes:
                cpus = [int(cpu) for cpu in node.get("cpus", [])]
                delta = _aggregate_node_delta(cpus, prev_ticks, ticks)
                if delta is None:
                    nodes.append(
                        _numa_node_usage_payload(
                            node,
                            online_cpus=len(cpus),
                            busy_cores=None,
                            utilization_pct=None,
                        )
                    )
                    continue
                busy_delta, total_delta, covered = delta
                utilization_pct = busy_delta / total_delta * 100.0
                busy_cores = (
                    busy_delta / self._user_hz / window_s
                    if window_s > 0 and self._user_hz > 0
                    else None
                )
                nodes.append(
                    _numa_node_usage_payload(
                        node,
                        online_cpus=covered,
                        busy_cores=round(busy_cores, 2) if busy_cores is not None else None,
                        utilization_pct=round(utilization_pct, 2),
                    )
                )

            return {
                "available": True,
                "sampled": True,
                "node_count": len(self._nodes),
                "window_s": round(window_s, 3),
                "user_hz": self._user_hz,
                "nodes": nodes,
            }
