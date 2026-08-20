"""Independent per-execution cgroup v2 resource artifact.

Writes the cgroup/process-sampler view of a tool execution into a standalone
``tool-resource/cgroup-resource-<execution_id>.json`` file next to the run's
trace. This is an intentionally *independent* measurement source from native
eBPF clause telemetry:

- cpu / memory / disk / network here come from the cgroup v2 + procfs
  sampler (``ToolRuntimeSample``), not from eBPF.
- cgroup v2 exposes no native network counter, so ``network_*_bytes_delta``
  are sourced by the sampler from the scope's processes (procfs-based) and
  are expected to roughly agree with an eBPF network view when one exists.

The ``span_end.resources.cgroup_artifact_path`` field references this file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from clawtune_sidecar.monitoring.tool_runtime import ToolRuntimeSample

_SCHEMA = "cgroup_resource_v1"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(name: str | None) -> str:
    if not name:
        return "unknown"
    return _UNSAFE.sub("_", name)


@dataclass(frozen=True)
class CgroupResourceResult:
    schema: str = _SCHEMA
    execution_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str = ""
    source: str = "cgroup-v2"
    monitor_source: str | None = None
    attribution_source: str | None = None
    ts_start: float | None = None
    ts_end: float | None = None
    duration_ms: int | None = None
    cpu_time_s: float | None = None
    cpu_utilization_avg_cores: float | None = None
    memory_rss_before_bytes: int | None = None
    memory_rss_after_bytes: int | None = None
    memory_rss_peak_bytes: int | None = None
    disk_read_bytes_delta: int | None = None
    disk_write_bytes_delta: int | None = None
    network_rx_bytes_delta: int | None = None
    network_tx_bytes_delta: int | None = None
    sampling_interval_ms: int | None = None
    sampling_point_count: int | None = None
    sampling_quality: str | None = None
    sampling_coverage_ms: int | None = None
    cpu_source: str | None = None
    memory_source: str | None = None
    disk_source: str | None = None
    network_source: str | None = None
    fallback_used: bool = False
    cgroup_setup_error: str | None = None
    cgroup_read_error: str | None = None
    collector_errors: tuple[str, ...] = ()
    independence: str = (
        "independent of native eBPF clause telemetry; "
        "network sourced via procfs sampler (cgroup v2 has no native counter)"
    )

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def build_cgroup_resource(
    sample: ToolRuntimeSample,
    *,
    execution_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    attribution_source: str | None = None,
) -> CgroupResourceResult:
    """Map a cgroup/process sampler snapshot into the standalone result."""
    cgroup_backed = sample.monitor_source == "cgroup-v2"
    process_source = "procfs-process-tree"
    network_source = (
        process_source
        if sample.net_rx_bytes_delta is not None or sample.net_tx_bytes_delta is not None
        else "unavailable"
    )
    return CgroupResourceResult(
        execution_id=execution_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        source=(
            "cgroup-v2"
            if sample.monitor_source == "cgroup-v2"
            else "process-tree"
        ),
        monitor_source=sample.monitor_source,
        attribution_source=attribution_source,
        ts_start=sample.started_at,
        ts_end=sample.ended_at,
        duration_ms=sample.duration_ms,
        cpu_time_s=sample.cpu_time_delta_s,
        cpu_utilization_avg_cores=sample.cpu_utilization_avg_cores,
        memory_rss_before_bytes=sample.rss_bytes_before,
        memory_rss_after_bytes=sample.rss_bytes_after,
        memory_rss_peak_bytes=sample.rss_bytes_peak,
        disk_read_bytes_delta=sample.read_bytes_delta,
        disk_write_bytes_delta=sample.write_bytes_delta,
        network_rx_bytes_delta=sample.net_rx_bytes_delta,
        network_tx_bytes_delta=sample.net_tx_bytes_delta,
        sampling_interval_ms=sample.sampling_interval_ms,
        sampling_point_count=sample.sampling_point_count,
        sampling_quality=sample.sampling_quality,
        sampling_coverage_ms=sample.monitor_duration_ms,
        cpu_source="cgroup-v2-cpu.stat" if cgroup_backed else process_source,
        memory_source="cgroup-v2-memory" if cgroup_backed else process_source,
        disk_source="cgroup-v2-io.stat" if cgroup_backed else process_source,
        network_source=network_source,
        fallback_used=not cgroup_backed or network_source == process_source,
    )


def write_cgroup_resource(
    trace_dir: Path,
    sample: ToolRuntimeSample | None,
    *,
    execution_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    attribution_source: str | None = None,
) -> str | None:
    """Write the independent cgroup artifact; return its trace-relative path.

    Returns ``None`` when there is no cgroup sample or execution id (e.g. the
    sampler was not cgroup-backed, or the tool is fully in-process with no
    execution), so no dangling reference is emitted.
    """
    if sample is None:
        return None
    if sample.monitor_source not in ("cgroup-v2", "psutil-process-tree"):
        return None
    if not execution_id:
        return None
    result = build_cgroup_resource(
        sample,
        execution_id=execution_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        attribution_source=attribution_source,
    )
    rel = Path("tool-resource") / f"cgroup-resource-{_safe(execution_id)}.json"
    path = Path(trace_dir) / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Trace emission must never fail because an auxiliary artifact could
        # not be written; the in-trace resources remain the source of truth.
        return None
    return rel.as_posix()
