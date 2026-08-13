"""Extract honest per-tool-call CPU and container-memory labels."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Sequence

from harness.container_stats_sampler import _parse_memory_mb
from trace_collect.tool_latency_dataset import (
    ToolLatencySample,
    discover_trace_files,
    extract_tool_latency_samples,
    require_explicit_trace_task_ids,
)
from trace_collect.trace_data import TraceData


_AMBIENT_MEMORY_TOLERANCE_S = 2.0
_CENSOR_RESULT_PREFIX = "Error: [timeout]"
_CENSOR_RESULT_MARKER = "Exit code: 124"


@dataclass(frozen=True)
class ResourceCallSample:
    """Observed resource labels for one canonical ``tool_exec`` action."""

    sample_id: str
    source_trace: str
    task_id: str
    agent_id: str
    instance_id: str
    iteration: int
    action_id: str
    tool_name: str
    tool_args: dict[str, Any] | None
    tool_ts_start: float
    tool_ts_end: float
    censored: bool
    cpu_core_seconds: float | None
    cpu_core_seconds_eligible: bool
    cpu_core_seconds_kind: str
    peak_cpu_cores: float | None
    peak_cpu_cores_eligible: bool
    peak_cpu_clipped_sample_count: int
    peak_memory_mb: float | None
    peak_memory_mb_eligible: bool
    ambient_memory_mb: float | None
    ambient_memory_mb_eligible: bool
    memory_window_sample_count: int
    ambient_before_mb: float | None
    ambient_before_age_s: float | None

    def to_json_obj(self) -> dict[str, Any]:
        """Return the dict-row shape consumed by the shared prior machinery."""

        return {field: getattr(self, field) for field in self.__dataclass_fields__}


def extract_resource_call_samples(trace_path: Path) -> list[ResourceCallSample]:
    """Align one trace's tool calls with CPU timelines and container samples."""

    trace_path = trace_path.resolve()
    trace = TraceData.load(trace_path)
    actions = {
        _action_key(action): action
        for action in trace.actions
        if action.get("action_type") == "tool_exec"
    }
    memory_samples = _load_memory_samples(trace_path.parent / "resources.json")
    memory_epochs = [epoch for epoch, _ in memory_samples]
    output: list[ResourceCallSample] = []
    for call in extract_tool_latency_samples(trace_path):
        action = actions.get((call.agent_id, call.iteration, call.action_id))
        if action is None:
            raise ValueError(f"{trace_path}: missing action for {call.sample_id}")
        data = action.get("data") or {}
        censored = _is_censored(data)
        (
            cpu_core_seconds,
            cpu_core_seconds_eligible,
            cpu_core_seconds_kind,
            peak_cpu_cores,
            peak_cpu_cores_eligible,
            clipped_count,
        ) = _cpu_labels(call, data, censored=censored)
        peak_memory_mb, ambient_memory_mb, window_count = _memory_labels(
            memory_samples,
            memory_epochs,
            call.tool_ts_start,
            call.tool_ts_end,
        )
        ambient_before_mb, ambient_before_age_s = _ambient_before(
            memory_samples, memory_epochs, call.tool_ts_start
        )
        if censored:
            peak_memory_mb = None
            ambient_memory_mb = None
        output.append(
            ResourceCallSample(
                sample_id=call.sample_id,
                source_trace=call.source_trace,
                task_id=call.task_id,
                agent_id=call.agent_id,
                instance_id=call.instance_id,
                iteration=call.iteration,
                action_id=call.action_id,
                tool_name=call.tool_name,
                tool_args=call.tool_args,
                tool_ts_start=call.tool_ts_start,
                tool_ts_end=call.tool_ts_end,
                censored=censored,
                cpu_core_seconds=cpu_core_seconds,
                cpu_core_seconds_eligible=cpu_core_seconds_eligible,
                cpu_core_seconds_kind=cpu_core_seconds_kind,
                peak_cpu_cores=peak_cpu_cores,
                peak_cpu_cores_eligible=peak_cpu_cores_eligible,
                peak_cpu_clipped_sample_count=clipped_count,
                peak_memory_mb=peak_memory_mb,
                peak_memory_mb_eligible=peak_memory_mb is not None,
                ambient_memory_mb=ambient_memory_mb,
                ambient_memory_mb_eligible=ambient_memory_mb is not None,
                memory_window_sample_count=window_count,
                ambient_before_mb=ambient_before_mb,
                ambient_before_age_s=ambient_before_age_s,
            )
        )
    return output


def load_resource_corpus(
    trace_root: Path,
    task_manifest: Path,
    *,
    limit_tasks: int | None = None,
) -> tuple[dict[str, list[ResourceCallSample]], list[str]]:
    """Load declared tasks while validating trace metadata and membership."""

    all_task_ids = _manifest_task_ids(task_manifest)
    task_ids = all_task_ids
    if limit_tasks is not None:
        if limit_tasks < 1:
            raise ValueError("--limit-tasks must be positive")
        task_ids = task_ids[:limit_tasks]
    trace_paths = discover_trace_files([trace_root])
    task_by_trace = require_explicit_trace_task_ids(trace_paths)
    declared_all = set(all_task_ids)
    observed = set(task_by_trace.values())
    if observed != declared_all:
        raise ValueError(
            "trace tasks differ from declared task_ids: "
            f"missing={sorted(declared_all - observed)}, "
            f"unexpected={sorted(observed - declared_all)}"
        )

    selected = set(task_ids)
    samples_by_task: dict[str, list[ResourceCallSample]] = defaultdict(list)
    for trace_path in trace_paths:
        expected_task = task_by_trace[str(trace_path.resolve())]
        if expected_task not in selected:
            continue
        for sample in extract_resource_call_samples(trace_path):
            if sample.task_id != expected_task:
                raise ValueError(
                    "resource sample task differs from trace metadata: "
                    f"{sample.source_trace}: {sample.task_id!r} != {expected_task!r}"
                )
            samples_by_task[expected_task].append(sample)
    return {task_id: samples_by_task[task_id] for task_id in task_ids}, task_ids


def _manifest_task_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    task_ids = payload.get("task_ids") if isinstance(payload, dict) else None
    if (
        not isinstance(task_ids, list)
        or not task_ids
        or any(
            not isinstance(task_id, str) or not task_id.strip() for task_id in task_ids
        )
    ):
        raise ValueError(f"{path}: task_ids must contain non-empty strings")
    normalized = sorted(task_id.strip() for task_id in task_ids)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{path}: task_ids contains duplicates")
    expected = payload.get("expected_task_count")
    if expected is not None and expected != len(normalized):
        raise ValueError(
            f"{path}: expected_task_count {expected!r} != {len(normalized)} task_ids"
        )
    return normalized


def _cpu_labels(
    call: ToolLatencySample,
    data: dict[str, Any],
    *,
    censored: bool,
) -> tuple[float | None, bool, str, float | None, bool, int]:
    if censored:
        return None, False, "censored", None, False, 0
    if call.tool_name != "exec":
        return 0.0, True, "non_exec_zero", None, False, 0
    timeline = data.get("resource_timeline")
    if timeline is None or (
        isinstance(timeline, dict) and timeline.get("telemetry_absent") is True
    ):
        return None, False, "missing_exec_timeline", None, False, 0
    if not isinstance(timeline, dict):
        raise ValueError(f"{call.sample_id}: resource_timeline must be an object")
    summary = timeline.get("summary")
    if not isinstance(summary, dict):
        raise ValueError(f"{call.sample_id}: resource_timeline.summary is missing")
    cpu_core_seconds = _nonnegative_number(
        summary.get("cpu_core_s"), f"{call.sample_id}: summary.cpu_core_s"
    )
    timeline_samples = timeline.get("samples")
    if not isinstance(timeline_samples, list):
        raise ValueError(f"{call.sample_id}: resource_timeline.samples must be a list")

    rates: list[float] = []
    clipped_count = 0
    for index, sample in enumerate(timeline_samples):
        if not isinstance(sample, dict):
            raise ValueError(f"{call.sample_id}: CPU sample {index} must be an object")
        dt_s = _positive_number(sample.get("dt_s"), f"{call.sample_id}: dt_s")
        interval_cpu = _nonnegative_number(
            sample.get("cpu_core_s"), f"{call.sample_id}: cpu_core_s"
        )
        quota = _positive_number(
            sample.get("cpu_quota_cores"),
            f"{call.sample_id}: cpu_quota_cores",
        )
        rate = interval_cpu / dt_s
        if rate > quota:
            rate = quota
            clipped_count += 1
        rates.append(rate)

    peak_eligible = len(rates) >= 2 and call.tool_ts_end - call.tool_ts_start >= 1.0
    return (
        cpu_core_seconds,
        True,
        "exec_timeline",
        max(rates) if peak_eligible else None,
        peak_eligible,
        clipped_count,
    )


def _load_memory_samples(path: Path) -> list[tuple[float, float]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: resources samples must be a list")
    samples: list[tuple[float, float]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: sample {index} must be an object")
        epoch = _nonnegative_number(row.get("epoch"), f"{path}: sample {index} epoch")
        mem_usage = row.get("mem_usage")
        if not isinstance(mem_usage, str):
            raise ValueError(f"{path}: sample {index} mem_usage must be a string")
        memory_mb = _parse_memory_mb(mem_usage)
        if memory_mb is not None:
            if not math.isfinite(memory_mb) or memory_mb < 0.0:
                raise ValueError(f"{path}: sample {index} has invalid memory")
            samples.append((epoch, memory_mb))
    samples.sort()
    return samples


def _memory_labels(
    samples: Sequence[tuple[float, float]],
    epochs: Sequence[float],
    ts_start: float,
    ts_end: float,
) -> tuple[float | None, float | None, int]:
    left = bisect_left(epochs, ts_start)
    right = bisect_right(epochs, ts_end)
    in_window = samples[left:right]
    if in_window:
        return max(memory_mb for _, memory_mb in in_window), None, len(in_window)
    candidates = []
    if left:
        candidates.append(samples[left - 1])
    if right < len(samples):
        candidates.append(samples[right])
    if not candidates:
        return None, None, 0
    epoch, memory_mb = min(
        candidates,
        key=lambda item: (_distance_to_window(item[0], ts_start, ts_end), item[0]),
    )
    if _distance_to_window(epoch, ts_start, ts_end) <= _AMBIENT_MEMORY_TOLERANCE_S:
        return None, memory_mb, 0
    return None, None, 0


def _ambient_before(
    samples: Sequence[tuple[float, float]],
    epochs: Sequence[float],
    ts_start: float,
) -> tuple[float | None, float | None]:
    index = bisect_left(epochs, ts_start) - 1
    if index < 0:
        return None, None
    epoch, memory_mb = samples[index]
    return memory_mb, ts_start - epoch


def _distance_to_window(epoch: float, ts_start: float, ts_end: float) -> float:
    if epoch < ts_start:
        return ts_start - epoch
    if epoch > ts_end:
        return epoch - ts_end
    return 0.0


def _is_censored(data: dict[str, Any]) -> bool:
    result = data.get("tool_result")
    return (
        data.get("success") is False
        and isinstance(result, str)
        and result.startswith(_CENSOR_RESULT_PREFIX)
        and _CENSOR_RESULT_MARKER in result
    )


def _action_key(action: dict[str, Any]) -> tuple[str, int, str]:
    iteration = action.get("iteration")
    if not isinstance(iteration, int):
        raise ValueError(f"action {action.get('action_id')!r} has invalid iteration")
    return (
        str(action.get("agent_id") or ""),
        iteration,
        str(action.get("action_id") or ""),
    )


def _nonnegative_number(value: Any, source: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"{source} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{source} must be finite and non-negative")
    return number


def _positive_number(value: Any, source: str) -> float:
    number = _nonnegative_number(value, source)
    if number <= 0.0:
        raise ValueError(f"{source} must be positive")
    return number


__all__ = [
    "ResourceCallSample",
    "extract_resource_call_samples",
    "load_resource_corpus",
]
