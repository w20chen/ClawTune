"""Strict v5 clause event adapter retaining measured execution timestamps."""

from __future__ import annotations

import json
import math
import shlex
from collections import Counter
from pathlib import Path

from clawtune_sidecar.tool_resource_commands import extract_command
from cold_start.flat_loader import _censored_call, recorded_clause_structure
from tool_resource.features import shell_bin_requires_exec_evidence
from tool_resource.runtime_kb import is_pipeline_dependent_consumer
from tool_time.edge_kappa_adapter import shell_query
from edge_kappa_kb import TimeOutcome, TrainingEvent
from clawtune_kb.store import digest


def read_v5_events(dataset: Path, task: dict, task_key: str) -> tuple[list[TrainingEvent], Counter]:
    """Only quality-gated clauses with exact duration and finite clocks train the KB."""
    events: list[TrainingEvent] = []
    excluded: Counter = Counter()
    seen_files: set[str] = set()
    namespace = task["benchmark"] + ":" + task["group"]
    for file in task["files"]:
        if file["version"] != 5:
            raise ValueError("trace-clock evaluation requires v5 task traces")
        path = dataset / file["path"]
        if digest(path) != file["sha256"]:
            raise ValueError(f"dataset changed: {file['path']}")
        if file["sha256"] in seen_files:
            continue
        seen_files.add(file["sha256"])
        seen_actions: set[str] = set()
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if '"tool_exec"' not in line:
                    continue
                row = json.loads(line)
                if row.get("type") != "action" or row.get("action_type") != "tool_exec":
                    continue
                if row.get("instance_id", task["task_id"]) != task["task_id"]:
                    raise ValueError("trace task identity mismatch")
                action_id = row.get("action_id")
                if not isinstance(action_id, str) or not action_id or action_id in seen_actions:
                    raise ValueError("missing or duplicate action ID")
                seen_actions.add(action_id)
                data = row.get("data") or {}
                if data.get("tool_name") not in {"exec", "terminal_exec"}:
                    continue
                if _censored_call(data):
                    excluded["censored_call_without_verified_clause_bound"] += 1
                    continue
                args = data.get("tool_args")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        continue
                command = extract_command(args)
                observation = data.get("resource_observation")
                if not isinstance(command, str) or not isinstance(observation, dict):
                    continue
                if (observation.get("tool_call_id") != data.get("tool_call_id") or
                        observation.get("command") != command):
                    raise ValueError("resource/action identity mismatch")
                if not isinstance(data.get("tool_call_id"), str) or not data["tool_call_id"]:
                    excluded["missing_call_id"] += 1
                    continue
                if (observation.get("eligible_for_kb") is not True or
                        observation.get("telemetry_quality") != "ok" or
                        observation.get("telemetry_status") != "ok"):
                    excluded["ineligible_resource_observation"] += 1
                    continue
                for index, clause in enumerate(recorded_clause_structure(
                        command, observation.get("clauses") or [], recorded_only=True)):
                    if clause.get("eligible_for_kb") is not True or clause.get("telemetry_quality") != "ok":
                        excluded["ineligible_clause"] += 1
                        continue
                    latency = clause.get("latency_ms")
                    if ((clause.get("availability") or {}).get("latency") != "ok" or
                            not isinstance(latency, (float, int)) or
                            not math.isfinite(latency) or latency < 0):
                        excluded["no_exact_clause_duration"] += 1
                        continue
                    argv = clause.get("argv")
                    bin_ = clause.get("bin")
                    if (not isinstance(argv, list) or not argv or
                            not all(isinstance(arg, str) for arg in argv) or
                            not isinstance(bin_, str) or not bin_):
                        excluded["invalid_clause"] += 1
                        continue
                    if is_pipeline_dependent_consumer(clause) or not shell_bin_requires_exec_evidence(
                            bin_, argv[0]):
                        excluded["unsupported_shell_clause"] += 1
                        continue
                    start, end = clause.get("ts_start"), clause.get("ts_end")
                    if (not isinstance(start, (float, int)) or not isinstance(end, (float, int)) or
                            not math.isfinite(start) or not math.isfinite(end) or end < start):
                        excluded["missing_clause_clock"] += 1
                        continue
                    command_clause = shlex.join(argv)
                    try:
                        query = shell_query(command_clause, repo=namespace)
                    except ValueError:
                        excluded["normalization_failed"] += 1
                        continue
                    call_id = str(data["tool_call_id"])
                    identity = f"{task_key}:{file['path']}:{action_id}:{index}"
                    outcome = TimeOutcome(identity, float(start), float(end), task_key,
                        call_id, str(index), duration_ms=float(latency), label_source="clause")
                    events.append(TrainingEvent(query, outcome, command_clause))
    return events, excluded
