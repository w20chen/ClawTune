from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description="Import agent-test-bench trace.jsonl into clawtune.v1 offline events.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profiles-out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = {"read": 0, "written": 0, "unmapped": 0}
    outputs: list[dict[str, Any]] = []
    profile_samples: dict[tuple[str, str | None], list[int]] = {}
    metadata: dict[str, Any] = {}

    for line in args.input.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        stats["read"] += 1
        record = json.loads(line)
        if record.get("type") == "trace_metadata":
            metadata = record
            continue
        if record.get("action_type") != "tool_exec":
            stats["unmapped"] += 1
            continue
        event = map_tool_exec(record, metadata)
        if event is None:
            stats["unmapped"] += 1
            continue
        outputs.append(event)
        profile_samples.setdefault((event["tool_name"], infer_operation(record)), []).append(event["duration_ms"])
        stats["written"] += 1

    print(json.dumps(stats, indent=2, sort_keys=True))
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(json.dumps(item, sort_keys=True) for item in outputs) + ("\n" if outputs else ""), encoding="utf-8")
        if args.profiles_out is not None:
            args.profiles_out.parent.mkdir(parents=True, exist_ok=True)
            args.profiles_out.write_text(
                json.dumps(build_profiles(profile_samples), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )


def map_tool_exec(record: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any] | None:
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    tool_name = data.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    start = as_float(record.get("ts_start") or data.get("ts_start"))
    end = as_float(record.get("ts_end") or data.get("ts_end"))
    duration_ms = 0 if start is None or end is None or end < start else int((end - start) * 1000)
    succeeded = data.get("success")
    if not isinstance(succeeded, bool):
        succeeded = not bool(data.get("error"))
    return {
        "schema_version": "clawtune.v1",
        "event_id": f"import-{uuid4()}",
        "occurred_at": datetime.fromtimestamp(end or start or datetime.now(timezone.utc).timestamp(), timezone.utc).isoformat().replace("+00:00", "Z"),
        "plugin_version": "import-agent-test-bench",
        "run_id": metadata.get("run_id"),
        "session_id": None,
        "session_key": None,
        "agent_id": record.get("agent_id"),
        "tool_call_id": record.get("action_id"),
        "decision_id": None,
        "lease_id": None,
        "execution_id": None,
        "tool_name": tool_name,
        "duration_ms": duration_ms,
        "succeeded": succeeded,
        "error_type": "tool_error" if data.get("error") else None,
        "error_digest": None,
        "result_size_bytes": None,
    }


def infer_operation(record: dict[str, Any]) -> str | None:
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    tool_name = data.get("tool_name")
    if isinstance(tool_name, str) and "pytest" in tool_name.lower():
        return "pytest"
    tool_args = _tool_args(data.get("tool_args"))
    command = tool_args.get("command") or tool_args.get("cmd")
    if not isinstance(command, str):
        return tool_name if isinstance(tool_name, str) else None
    lowered = command.lower()
    if "pytest" in lowered:
        return "pytest"
    if "python" in lowered or "python3" in lowered:
        return "python"
    parts = command.strip().split()
    return Path(parts[0]).name if parts else None


def build_profiles(samples: dict[tuple[str, str | None], list[int]]) -> dict[str, Any]:
    profiles = []
    for (tool_name, operation), durations in sorted(samples.items()):
        if not durations:
            continue
        ordered = sorted(durations)
        profiles.append(
            {
                "tool_name": tool_name,
                "operation": operation,
                "sample_count": len(ordered),
                "duration_p50_ms": _percentile(ordered, 0.5),
                "duration_p90_ms": _percentile(ordered, 0.9),
            }
        )
    return {"profiles": profiles}


def _tool_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"command": value}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    index = min(len(values) - 1, max(0, int((len(values) - 1) * quantile)))
    return values[index]


def as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


if __name__ == "__main__":
    main()
