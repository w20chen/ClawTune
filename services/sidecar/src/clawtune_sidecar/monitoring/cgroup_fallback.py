"""Dedicated cgroup counter fallback; never reinterpret memory charge as RSS.

Two cheap snapshots bracket the collector window, including when eBPF fails
at completion. These values describe that window, not an inferred action
window, and are not action training labels. No background sampler is added.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clawtune_sidecar.contracts.models import ResourceScope
from clawtune_sidecar.monitoring.process import ProcessResourceSampler


def _read(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "identity": (stat.st_dev, stat.st_ino),
        "cpu": ProcessResourceSampler._read_cgroup_cpu_usec(path),
        "io": ProcessResourceSampler._read_cgroup_io_stat(path),
        "memory_peak": ProcessResourceSampler._read_int_file(path / "memory.peak"),
        "time": time.monotonic_ns(),
    }


@dataclass
class CgroupFallbackWindow:
    path: Path
    before: dict[str, Any]

    @classmethod
    def open(cls, scope: ResourceScope | None) -> CgroupFallbackWindow | None:
        if (scope is None or scope.kind != "cgroup-v2" or not scope.cgroup_path
                or scope.attribution_source != "exclusive-execution-cgroup"):
            return None
        path = Path(scope.cgroup_path)
        if path.as_posix().rstrip("/") in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}:
            return None
        try:
            return cls(path, _read(path))
        except (OSError, ValueError):
            return None

    def finish(self) -> dict[str, Any] | None:
        from clawtune_sidecar.monitoring.ebpf_tool import unavailable_observation

        try:
            after = _read(self.path)
        except (OSError, ValueError):
            return None
        if after["identity"] != self.before["identity"]:
            return None
        result = unavailable_observation(
            "cgroup_collector_window_only", attribution="exclusive_process_tree",
            started_ns=self.before["time"], ended_ns=after["time"],
        )
        result.pop("unavailable_reason")
        result.update(backend="cgroup-v2", fallback_used=True, scope="cgroup-v2")
        result["window"].update(
            observed_start_ns=str(self.before["time"]), observed_end_ns=str(after["time"]),
            complete=False, kind="collector",
        )
        metrics = result["metrics"]
        cpu_before, cpu_after = self.before["cpu"], after["cpu"]
        if cpu_before is not None and cpu_after is not None and cpu_after >= cpu_before:
            metrics["cpu_time"].update(
                available=True, reason="collector_window_only", measurement="cgroup_v2_cpu_stat",
                value_seconds=(cpu_after - cpu_before) / 1e6, average_cores=None,
            )
        io_before, io_after = self.before["io"], after["io"]
        if (io_before is not None and io_after is not None
                and all(b >= a for a, b in zip(io_before, io_after))):
            metrics["disk_io"].update(
                available=True, reason="collector_window_only", measurement="cgroup_v2_io_stat",
                read_bytes=io_after[0] - io_before[0], write_bytes=io_after[1] - io_before[1],
            )
        for name, reason in (("memory_peak", "cgroup_memory_charge_is_not_rss"),
                             ("cpu_peak", "cgroup_peak_not_sampled"),
                             ("network_io", "cgroup_has_no_network_counter")):
            metrics[name]["reason"] = reason
        # A strictly increased lifetime high-water mark was necessarily set
        # within this window. An unchanged mark could predate it; do not reuse it.
        before_peak, after_peak = self.before["memory_peak"], after["memory_peak"]
        if before_peak is not None and after_peak is not None and after_peak > before_peak:
            metrics["memory_charge_peak"] = {
                "available": True, "eligible": False, "reason": "collector_window_only",
                "measurement": "cgroup_v2_memory_peak_charge", "value_bytes": after_peak,
            }
        return result if any(m["available"] for m in metrics.values()) else None
