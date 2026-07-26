from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CURRENT_TRACE_FORMAT_VERSION = 5


@dataclass
class TraceData:
    path: Path
    metadata: dict[str, Any]
    actions: list[dict[str, Any]]
    events: list[dict[str, Any]]
    summaries: list[dict[str, Any]]
    agents: list[str]

    @classmethod
    def load(cls, path: Path, agent_filter: str | None = None) -> "TraceData":
        metadata: dict[str, Any] = {}
        actions: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        seen_agents: dict[str, None] = {}

        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                record = json.loads(line)
                rec_type = record.get("type", "")
                agent_id = record.get("agent_id")
                if agent_filter is not None and agent_id is not None and agent_filter not in agent_id:
                    continue
                if agent_id is not None:
                    seen_agents[str(agent_id)] = None
                if rec_type == "trace_metadata":
                    metadata.update(record)
                elif rec_type == "action":
                    actions.append(record)
                elif rec_type == "event":
                    events.append(record)
                elif rec_type == "summary":
                    summaries.append(record)
                else:
                    raise ValueError(
                        f"Unsupported record type {rec_type!r} in {path}:{lineno}; "
                        "expected a canonical trace JSONL."
                    )

        actions.sort(key=lambda r: (r.get("iteration", 0), r.get("ts_start", 0)))
        events.sort(key=lambda r: r.get("ts", 0.0))
        if not metadata:
            raise ValueError(f"Missing trace_metadata record in {path}.")
        if metadata.get("trace_format_version") != CURRENT_TRACE_FORMAT_VERSION:
            raise ValueError(
                f"Unsupported trace_format_version {metadata.get('trace_format_version')!r} "
                f"in {path}; expected {CURRENT_TRACE_FORMAT_VERSION}."
            )
        return cls(path, metadata, actions, events, summaries, list(seen_agents.keys()))
