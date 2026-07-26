"""Minimal trace latency helpers required by tool_resource.labels."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Iterable

from trace_collect.trace_data import TraceData


@dataclass(frozen=True)
class ToolLatencySample:
    sample_id: str
    source_trace: str
    task_id: str
    agent_id: str
    instance_id: str
    iteration: int
    action_id: str
    tool_name: str
    tool_call_id: str
    tool_ts_start: float
    tool_ts_end: float
    latency_ms: float
    success: bool | None
    reported_duration_ms: float | None
    tool_args: dict[str, Any] | None = None
    tool_name_missing: bool = False


def discover_trace_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("trace.jsonl"))
            files.extend(path for path in root.rglob("*.jsonl") if path.name != "trace.jsonl")
    return sorted(set(files))


def extract_tool_latency_samples(
    trace_path: Path,
    *,
    agent_filter: str | None = None,
) -> list[ToolLatencySample]:
    trace = TraceData.load(trace_path, agent_filter=agent_filter)
    task_id = str(trace.metadata.get("instance_id") or trace_path)
    samples: list[ToolLatencySample] = []
    for action in trace.actions:
        if action.get("action_type") != "tool_exec":
            continue
        data = action.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        ts_start = _float_field(action, "ts_start")
        ts_end = _float_field(action, "ts_end")
        if not math.isfinite(ts_start) or not math.isfinite(ts_end) or ts_end < ts_start:
            raise ValueError(f"{trace_path}: invalid tool action timestamps")
        action_id = str(action.get("action_id") or "")
        agent_id = str(action.get("agent_id") or "")
        iteration = _int_field(action, "iteration")
        tool_name = _tool_name(action)
        tool_name_missing = not tool_name
        if tool_name_missing:
            tool_name = "__missing_tool_name__"
        samples.append(
            ToolLatencySample(
                sample_id=f"{trace_path}:{agent_id}:{iteration}:{action_id}",
                source_trace=str(trace_path),
                task_id=task_id,
                agent_id=agent_id,
                instance_id=str(action.get("instance_id") or trace.metadata.get("instance_id") or ""),
                iteration=iteration,
                action_id=action_id,
                tool_name=tool_name,
                tool_call_id=str(data.get("tool_call_id") or action_id),
                tool_ts_start=ts_start,
                tool_ts_end=ts_end,
                latency_ms=(ts_end - ts_start) * 1000.0,
                success=_optional_bool(data.get("success")),
                reported_duration_ms=_optional_float(data.get("duration_ms")),
                tool_args=_parse_tool_args(data.get("tool_args")),
                tool_name_missing=tool_name_missing,
            )
        )
    return samples


def require_explicit_trace_task_ids(trace_paths: Iterable[Path]) -> dict[str, str]:
    task_by_trace: dict[str, str] = {}
    for trace_path in trace_paths:
        trace = TraceData.load(trace_path)
        task_id = str(trace.metadata.get("instance_id") or "").strip()
        if not task_id:
            raise ValueError(f"trace lacks explicit metadata instance_id: {trace_path}")
        task_by_trace[str(trace_path.resolve())] = task_id
    return task_by_trace


def _parse_tool_args(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _float_field(row: dict[str, Any], field: str) -> float:
    value = row.get(field)
    return float(value) if isinstance(value, int | float) else math.nan


def _int_field(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _tool_name(action: dict[str, Any]) -> str:
    data = action.get("data")
    if isinstance(data, dict):
        for key in ("tool_name", "toolName", "name"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    value = action.get("tool_name")
    return value if isinstance(value, str) else ""


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise ValueError(f"expected numeric optional value, got {value!r}")
    return float(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"expected bool optional value, got {value!r}")
    return value
