"""Legacy dataset parsing adapter.

Reads the per-task layout produced by an external benchmark harness and turns
it into the exact input objects ClawTune's prediction algorithms consume.

Legacy layout (per task ``<org>__<repo>-<pr>``)::

    <task_dir>/
      attempt_1/
        clause_telemetry.json   # Stage-2 clause artifact (ClawTune-valid)
        trace.jsonl             # action-level trace (llm_call + tool_exec)
        ...                     # resources.json etc. (unused here)

The clause artifact is structurally identical to ClawTune's own Stage-2
clause telemetry, so this module reuses the native validator
(``tool_resource.sdk._load_valid_artifact``) verbatim and extracts
``ClauseEvent`` records (one per measured executable clause).  ``trace.jsonl``
supplies the call-level view (tool name, command, duration, success, real
timestamps, ``tool_call_id``) which the clause artifact alone does not carry.

Two important legacy-specific facts are preserved so the evaluator can reason
about them:

* clause rows in the artifact carry no ``ts_start``/``ts_end`` (the native
  loader synthesises ``0.0``/``latency``), so per-task causality is not
  available from the artifact alone;
* ``resources.json`` contains no per-call memory samples (monitoring was
  disabled), so continuous memory predictions cannot be anchored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from legacy_eval._bootstrap import ensure_paths

ensure_paths()

from agent_scheduler.tool_resource_commands import extract_command  # noqa: E402
from tool_resource.sdk import _load_valid_artifact  # noqa: E402

_TASK_NAME_RE = re.compile(r".*__.*-\d+$")
_ATTEMPT_NAME_RE = re.compile(r"^attempt_\d+$")


@dataclass(frozen=True)
class ClauseEvent:
    """One executable clause with a measured latency (legacy ground truth)."""

    repo: str
    bin: str
    argv: tuple[str, ...]
    latency_ms: float
    eligible: bool
    tool_call_id: str | None
    peak_cpu_cores: float | None
    sampled_peak_rss_mb: float | None
    command: str | None = None


@dataclass(frozen=True)
class ToolCallEvent:
    """One tool invocation from the action-level trace."""

    repo: str
    tool_name: str
    tool_args: str | None
    command: str | None
    duration_ms: float | None
    success: bool
    ts_start: float
    ts_end: float
    tool_call_id: str | None
    iteration: int
    peak_cpu_cores: float | None = None


@dataclass
class TaskArtifacts:
    """Everything loaded for one legacy task."""

    task_id: str
    task_dir: Path
    clause_events: list[ClauseEvent] = field(default_factory=list)
    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    rejected_attempts: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.clause_events) or bool(self.tool_calls)


def is_task_dir(name: str) -> bool:
    """True for ``<org>__<repo>-<pr>`` task directories."""

    return bool(name) and bool(_TASK_NAME_RE.match(name))


def discover_task_dirs(dataset_dir: str | Path) -> list[Path]:
    """Return task directories under *dataset_dir* in sorted order."""

    root = Path(dataset_dir)
    if not root.is_dir():
        raise ValueError(f"dataset directory does not exist: {root}")
    return sorted(
        entry
        for entry in root.iterdir()
        if entry.is_dir() and is_task_dir(entry.name)
    )


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_clause_artifact(path: Path, repo: str) -> list[ClauseEvent]:
    """Validate one legacy clause artifact and extract measured clauses.

    Raises ``ValueError`` when the artifact does not pass ClawTune's native
    Stage-2 validation (identical schema), so invalid artifacts are reported
    rather than silently skipped.
    """

    artifact = _load_valid_artifact(path)
    events: list[ClauseEvent] = []
    for call in artifact.get("calls", []):
        if not isinstance(call, dict):
            continue
        call_id = call.get("tool_call_id")
        call_id = call_id if isinstance(call_id, str) and call_id else None
        command = call.get("command")
        command = command if isinstance(command, str) else None
        call_eligible = call.get("eligible_for_kb") is True
        for clause in call.get("clauses", []):
            if not isinstance(clause, dict):
                continue
            availability = clause.get("availability")
            if not isinstance(availability, dict):
                continue
            if availability.get("latency") != "ok":
                continue
            latency_ms = clause.get("latency_ms")
            if isinstance(latency_ms, bool) or not isinstance(
                latency_ms, (int, float)
            ):
                continue
            bin_ = clause.get("bin")
            argv = clause.get("argv")
            if not isinstance(bin_, str) or not bin_:
                continue
            if not isinstance(argv, list) or not all(
                isinstance(arg, str) for arg in argv
            ):
                continue
            clause_eligible = clause.get("eligible_for_kb") is True
            events.append(
                ClauseEvent(
                    repo=repo,
                    bin=bin_,
                    argv=tuple(argv),
                    latency_ms=float(latency_ms),
                    eligible=call_eligible and clause_eligible,
                    tool_call_id=call_id,
                    peak_cpu_cores=_opt_float(clause.get("peak_cpu_cores")),
                    sampled_peak_rss_mb=_opt_float(
                        clause.get("sampled_peak_rss_mb")
                    ),
                    command=command,
                )
            )
    return events


def parse_trace(path: Path, repo: str) -> list[ToolCallEvent]:
    """Extract ``tool_exec`` actions from an action-level trace.jsonl."""

    calls: list[ToolCallEvent] = []
    with path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") != "action":
                continue
            if record.get("action_type") != "tool_exec":
                continue
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            tool_name = data.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            duration_ms = data.get("duration_ms")
            if isinstance(duration_ms, bool) or not isinstance(
                duration_ms, (int, float)
            ):
                duration_ms = None
            tool_args = data.get("tool_args")
            command = extract_command(tool_args) if tool_args is not None else None
            call_id = data.get("tool_call_id")
            calls.append(
                ToolCallEvent(
                    repo=repo,
                    tool_name=tool_name,
                    tool_args=tool_args if isinstance(tool_args, str) else None,
                    command=command,
                    duration_ms=(
                        float(duration_ms) if duration_ms is not None else None
                    ),
                    success=bool(data.get("success")),
                    ts_start=_opt_float(record.get("ts_start")) or 0.0,
                    ts_end=_opt_float(record.get("ts_end")) or 0.0,
                    tool_call_id=call_id if isinstance(call_id, str) else None,
                    iteration=int(record.get("iteration", 0) or 0),
                )
            )
    return calls


def _attempt_dirs(task_dir: Path) -> list[Path]:
    candidates = [
        entry
        for entry in task_dir.iterdir()
        if entry.is_dir() and _ATTEMPT_NAME_RE.match(entry.name)
    ]
    if candidates:
        return sorted(candidates)
    return [task_dir]  # fallback: files directly under the task directory


def load_task(task_dir: str | Path) -> TaskArtifacts:
    """Load all usable attempts for one legacy task directory."""

    task_dir = Path(task_dir)
    task_id = task_dir.name
    artifacts = TaskArtifacts(task_id=task_id, task_dir=task_dir)
    for attempt in _attempt_dirs(task_dir):
        clause_path = attempt / "clause_telemetry.json"
        trace_path = attempt / "trace.jsonl"
        try:
            events = (
                parse_clause_artifact(clause_path, task_id)
                if clause_path.is_file()
                else []
            )
        except Exception as exc:  # noqa: BLE001 - report and continue
            artifacts.rejected_attempts.append(
                f"{attempt.name}: clause artifact: {exc}"
            )
            events = []
        calls = (
            parse_trace(trace_path, task_id) if trace_path.is_file() else []
        )
        artifacts.clause_events.extend(events)
        artifacts.tool_calls.extend(calls)

    # Attach each tool call's peak CPU (max over its measured clauses) so the
    # continuous CPU target has call-level samples when available.
    cpu_by_call: dict[str, list[float]] = {}
    for ev in artifacts.clause_events:
        if ev.tool_call_id and ev.peak_cpu_cores is not None:
            cpu_by_call.setdefault(ev.tool_call_id, []).append(ev.peak_cpu_cores)
    enriched: list[ToolCallEvent] = []
    for call in artifacts.tool_calls:
        cpu = None
        if call.tool_call_id and call.tool_call_id in cpu_by_call:
            cpu = max(cpu_by_call[call.tool_call_id])
        enriched.append(replace(call, peak_cpu_cores=cpu))
    artifacts.tool_calls = enriched
    return artifacts


def load_all(
    dataset_dir: str | Path,
    *,
    task_ids: Iterable[str] | None = None,
) -> dict[str, TaskArtifacts]:
    """Load every legacy task under *dataset_dir*.

    When *task_ids* is given, only those tasks are returned (unknown ids are
    ignored rather than erroring, so a filtered load cannot crash on a stale
    split manifest).
    """

    wanted = None if task_ids is None else frozenset(task_ids)
    tasks: dict[str, TaskArtifacts] = {}
    for task_dir in discover_task_dirs(dataset_dir):
        tid = task_dir.name
        if wanted is not None and tid not in wanted:
            continue
        tasks[tid] = load_task(task_dir)
    return tasks


__all__ = [
    "ClauseEvent",
    "TaskArtifacts",
    "ToolCallEvent",
    "discover_task_dirs",
    "is_task_dir",
    "load_all",
    "load_task",
    "parse_clause_artifact",
    "parse_trace",
]
