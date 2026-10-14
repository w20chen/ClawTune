"""V5 action replay adapter; v6 decoding and ordinary execution stay separate."""
from __future__ import annotations

import hashlib
import json
import math
import shlex
from dataclasses import replace
from pathlib import Path

from swe_rebench.replay import ReplayModelTurn, ReplayPlan, ReplayToolCall, ReplayTraceError


def select_v5_paths(paths: list[Path], *, task_id: str | None, directory: bool) -> list[Path] | None:
    selected, versions = [], set()
    for path in paths:
        with path.open(encoding="utf-8-sig") as stream:
            try:
                first = next((json.loads(line) for line in stream if line.strip()), {})
            except ValueError as exc:
                raise ReplayTraceError(f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(first, dict):
            continue
        version = first.get("trace_format_version", first.get("schema_version"))
        identity = first.get("instance_id", first.get("task_id"))
        if version == 5 and task_id and identity and identity != task_id:
            if directory:
                continue
            raise ReplayTraceError(f"source task {identity!r} does not match {task_id!r}")
        if version is not None:
            versions.add(version)
        if version == 5:
            selected.append(path)
    if not selected:
        return None
    if versions != {5}:
        raise ReplayTraceError("cannot combine v5 actions with other trace versions")
    return selected


def _object(value, location):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ReplayTraceError(f"{location}: invalid JSON object") from exc
    if not isinstance(value, dict):
        raise ReplayTraceError(f"{location}: expected object or JSON object string")
    return value


def load_v5_plan(paths: list[Path]) -> ReplayPlan:
    digest, actions, identities = hashlib.sha256(), [], set()
    metadata = {}
    for path in paths:
        data = path.read_bytes()
        digest.update(data)
        for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            record = _object(line, f"{path}:{number}")
            identity = record.get("instance_id", record.get("task_id"))
            if identity:
                identities.add(identity)
            if record.get("type", record.get("record_type")) == "trace_metadata":
                if record.get("trace_format_version") != 5:
                    raise ReplayTraceError("expected v5 metadata")
                metadata = record
            elif record.get("schema_version") is not None:
                raise ReplayTraceError("cannot combine v5 actions with other trace versions")
            elif record.get("action_type") in {"llm_call", "tool_exec"}:
                actions.append(record)
    if len(identities) > 1:
        raise ReplayTraceError("multiple source tasks; select one task with --task-id")
    if all(type(r.get("ts_start")) in (int, float) and math.isfinite(r["ts_start"]) for r in actions):
        actions.sort(key=lambda r: r["ts_start"])
    turns, action_ids, executed_ids = [], set(), set()
    pending: list[ReplayToolCall] = []
    matched: set[int] = set()
    incomplete = 0
    for record in actions:
        action_id = record.get("action_id")
        if not isinstance(action_id, str) or not action_id or action_id in action_ids:
            raise ReplayTraceError("missing or duplicate action ID; select one recorded attempt")
        action_ids.add(action_id)
        data = _object(record.get("data"), action_id)
        if record["action_type"] == "llm_call":
            incomplete += len(pending) - len(matched)
            raw = _object(data.get("raw_response"), action_id)
            try:
                message = _object(raw["choices"][0]["message"], action_id)
            except (KeyError, IndexError, TypeError) as exc:
                raise ReplayTraceError(f"{action_id}: missing recorded model response") from exc
            pending, matched = [], set()
            for index, item in enumerate(message.get("tool_calls") or []):
                function = _object(item.get("function"), action_id)
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    raise ReplayTraceError(f"{action_id}: missing tool name")
                pending.append(ReplayToolCall(str(item.get("id") or f"{action_id}-{index}"), name,
                    _object(function.get("arguments"), action_id), action_id))
            if len({call.span_id for call in pending}) != len(pending):
                raise ReplayTraceError(f"{action_id}: duplicate requested tool ID")
            start, end = record.get("ts_start"), record.get("ts_end")
            if not (all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)) and end >= start):
                raise ReplayTraceError(f"{action_id}: invalid model duration")
            turns.append(ReplayModelTurn(action_id, str(raw.get("model") or metadata.get("model") or "replay-model"),
                round((end - start) * 1e9), message.get("content"), tuple(pending)))
        else:
            call_id, name = data.get("tool_call_id"), data.get("tool_name")
            args = _object(data.get("tool_args"), action_id)
            if not isinstance(call_id, str) or not call_id or call_id in executed_ids:
                raise ReplayTraceError(f"{action_id}: missing or duplicate executed tool ID")
            executed_ids.add(call_id)
            # Providers may replace placeholder IDs at dispatch. Fall back to
            # exact name/arguments only within this model turn, matching once.
            index = next((i for i, c in enumerate(pending) if c.span_id == call_id), None)
            if index is None:
                index = next((i for i, c in enumerate(pending) if i not in matched
                              and c.name == name and c.arguments == args), None)
            if index is None or index in matched:
                raise ReplayTraceError(f"{action_id}: no matching model request")
            call = pending[index]
            if call.name != name or call.arguments != args:
                raise ReplayTraceError(f"{action_id}: executed tool differs from recorded request")
            matched.add(index)
            calls = list(turns[-1].tool_calls)
            calls[index] = replace(call, span_id=call_id)
            turns[-1] = replace(turns[-1], tool_calls=tuple(calls))
    incomplete += len(pending) - len(matched)
    if not turns:
        raise ReplayTraceError("v5 trace contains no recorded model turns")
    return ReplayPlan(tuple(paths), digest.hexdigest(), str(metadata.get("run_id") or metadata.get("instance_id") or "replay-v5"),
                      tuple(turns), sum(len(t.tool_calls) for t in turns), incomplete, "v5")


def adapt_v5_plan(plan: ReplayPlan) -> ReplayPlan:
    if plan.source_format != "v5":
        return plan
    turns = []
    for turn in plan.turns:
        if not turn.tool_calls:
            turns.append(turn)
        for index, call in enumerate(turn.tool_calls):
            name, args = call.name, dict(call.arguments)
            if name in {"exec", "terminal_exec"}:
                name = "exec"
                directories = [args.pop(key, None) for key in ("working_dir", "workdir", "cwd")]
                cwd = next((value for value in directories if value), "/testbed")
                command = args.get("command", args.pop("cmd", None))
                if not isinstance(cwd, str) or not isinstance(command, str):
                    raise ReplayTraceError(f"{call.span_id}: invalid shell arguments")
                if "timeout_seconds" in args:
                    args.setdefault("timeout", args.pop("timeout_seconds"))
                args["command"] = f"cd -- {shlex.quote(cwd)} || exit $?\n{command}"
            elif name == "message":
                name = "clawtune_replay_message"
            elif name not in {"read_file", "write_file", "edit_file", "list_dir"}:
                raise ReplayTraceError(f"unsupported v5 replay tool: {name}")
            # Recorded v5 batches execute in order; prevent parallel edits and
            # consumers when dispatching through the current agent runtime.
            turns.append(replace(turn, tool_calls=(replace(call, name=name, arguments=args),),
                span_id=turn.span_id if index == 0 else f"{turn.span_id}-dispatch-{index}",
                duration_ns=turn.duration_ns if index == 0 else 0, content=turn.content if index == 0 else None))
    if turns[-1].tool_calls:
        turns.append(ReplayModelTurn("replay-v5-end", plan.model, 0, "Replay complete.", ()))
    return replace(plan, turns=tuple(turns))
