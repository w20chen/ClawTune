"""Deterministic LLM mock and replay plan for SWE-Rebench traces.

The replay server intentionally speaks the OpenAI-compatible subset consumed by
OpenClaw. It returns recorded model decisions after sleeping for the recorded
LLM duration; tool execution remains owned by OpenClaw's normal sandbox and
ClawTune plugin lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


class ReplayTraceError(ValueError):
    """The source trace cannot be replayed safely."""


@dataclass(frozen=True)
class ReplayToolCall:
    span_id: str
    name: str
    arguments: dict[str, Any]
    parent_span_id: str | None


@dataclass(frozen=True)
class ReplayModelTurn:
    span_id: str
    model: str
    duration_ns: int
    content: Any
    tool_calls: tuple[ReplayToolCall, ...]


@dataclass(frozen=True)
class ReplayPlan:
    source_paths: tuple[Path, ...]
    source_sha256: str
    trace_id: str
    turns: tuple[ReplayModelTurn, ...]
    tool_count: int
    incomplete_tool_count: int

    @property
    def model(self) -> str:
        return self.turns[0].model if self.turns else "replay-model"


def discover_trace_paths(value: str | Path) -> list[Path]:
    path = Path(value)
    if path.is_dir():
        paths = sorted(path.glob("*.jsonl"))
    else:
        paths = [path]
    if not paths or any(not path.is_file() for path in paths):
        raise ReplayTraceError(f"trace JSONL file(s) not found: {value}")
    return paths


def load_replay_plan(value: str | Path) -> ReplayPlan:
    paths = discover_trace_paths(value)
    records: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in paths:
        data = path.read_bytes()
        digest.update(data)
        for line_number, line in enumerate(data.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayTraceError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ReplayTraceError(f"non-object record in {path}:{line_number}")
            if record.get("schema_version") != 6:
                raise ReplayTraceError(
                    f"{path}:{line_number} is not trace v6; replay currently supports only schema_version=6"
                )
            records.append(record)

    starts: dict[str, dict[str, Any]] = {}
    ends: dict[str, dict[str, Any]] = {}
    trace_id: str | None = None
    for record in records:
        record_type = record.get("record_type")
        if record_type == "trace_metadata":
            if record.get("trace_format_version") != 6:
                raise ReplayTraceError("trace metadata does not declare format version 6")
            continue
        if record_type not in {"span_start", "span_end"}:
            continue
        span_id = record.get("span_id")
        if not isinstance(span_id, str) or not span_id:
            raise ReplayTraceError("v6 span is missing a non-empty span_id")
        trace_id = trace_id or record.get("trace_id")
        target = starts if record_type == "span_start" else ends
        if span_id in target:
            raise ReplayTraceError(f"duplicate {record_type} for span {span_id}")
        target[span_id] = record

    llm_spans: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for span_id, start in starts.items():
        if start.get("kind") != "llm":
            continue
        end = ends.get(span_id)
        if end is None:
            raise ReplayTraceError(f"LLM span {span_id} has no span_end output")
        llm_spans.append((start, end))
    llm_spans.sort(key=lambda pair: _span_order(pair[0]))
    if not llm_spans:
        raise ReplayTraceError("trace contains no complete LLM spans")

    tool_starts = [record for record in starts.values() if record.get("kind") == "tool"]
    tool_starts.sort(key=_span_order)
    tool_calls_by_parent: dict[str, list[ReplayToolCall]] = {}
    orphan_tools: list[ReplayToolCall] = []
    incomplete_tool_count = 0
    for start in tool_starts:
        call = _tool_call_from_start(start)
        if call is None:
            raise ReplayTraceError(
                f"tool span {start.get('span_id')!r} has no replayable requested arguments"
            )
        if call.span_id not in ends:
            incomplete_tool_count += 1
        if call.parent_span_id:
            tool_calls_by_parent.setdefault(call.parent_span_id, []).append(call)
        else:
            orphan_tools.append(call)

    turns: list[ReplayModelTurn] = []
    for start, end in llm_spans:
        span_id = str(start["span_id"])
        calls = list(tool_calls_by_parent.get(span_id, ()))
        if orphan_tools:
            # Older or partially correlated traces can omit parent_span_id.
            # Attach only calls that occur after this LLM and before the next
            # LLM; this preserves the observed causal order without inventing
            # a new agent loop.
            previous = _span_order(start)
            next_order = _span_order(llm_spans[len(turns) + 1][0]) if len(turns) + 1 < len(llm_spans) else None
            for call in orphan_tools:
                source = starts[call.span_id]
                order = _span_order(source)
                if order > previous and (next_order is None or order < next_order):
                    calls.append(call)
        calls.sort(key=lambda call: _span_order(starts[call.span_id]))
        turns.append(
            ReplayModelTurn(
                span_id=span_id,
                model=str(start.get("name") or "replay-model"),
                duration_ns=_duration_ns(start, end),
                content=_output_content(end),
                tool_calls=tuple(_dedupe_calls(calls)),
            )
        )

    return ReplayPlan(
        source_paths=tuple(paths),
        source_sha256=digest.hexdigest(),
        trace_id=str(trace_id or "replay-trace"),
        turns=tuple(turns),
        tool_count=len(tool_starts),
        incomplete_tool_count=incomplete_tool_count,
    )


def _span_order(record: dict[str, Any]) -> tuple[int, int, str]:
    sequence = record.get("sequence_no")
    try:
        sequence_value = int(sequence)
    except (TypeError, ValueError):
        sequence_value = 2**31
    try:
        monotonic = int(record.get("monotonic_time_ns", 0))
    except (TypeError, ValueError):
        monotonic = 0
    return sequence_value, monotonic, str(record.get("span_id") or "")


def _duration_ns(start: dict[str, Any], end: dict[str, Any]) -> int:
    value = end.get("duration_ns")
    try:
        duration = int(value)
    except (TypeError, ValueError):
        try:
            duration = int(end.get("monotonic_time_ns", 0)) - int(start.get("monotonic_time_ns", 0))
        except (TypeError, ValueError):
            duration = 0
    return max(0, duration)


def _tool_call_from_start(record: dict[str, Any]) -> ReplayToolCall | None:
    span_id = record.get("span_id")
    name = record.get("name")
    if not isinstance(span_id, str) or not isinstance(name, str) or not name:
        return None
    input_data = record.get("input")
    requested = input_data.get("requested_args") if isinstance(input_data, dict) else None
    if not isinstance(requested, dict):
        return None
    return ReplayToolCall(
        span_id=span_id,
        name=name,
        arguments=requested,
        parent_span_id=record.get("parent_span_id") if isinstance(record.get("parent_span_id"), str) else None,
    )


def _output_content(end: dict[str, Any]) -> Any:
    output = end.get("output")
    return output.get("content") if isinstance(output, dict) else ""


def _dedupe_calls(calls: Iterable[ReplayToolCall]) -> list[ReplayToolCall]:
    result: list[ReplayToolCall] = []
    seen: set[str] = set()
    for call in calls:
        if call.span_id not in seen:
            seen.add(call.span_id)
            result.append(call)
    return result


class ReplayLLMServer:
    """Threaded local OpenAI-compatible server for one replay plan."""

    def __init__(self, plan: ReplayPlan, *, timing: str = "exact", scale: float = 1.0) -> None:
        if timing not in {"exact", "scale", "none"}:
            raise ValueError(f"unsupported replay timing mode: {timing}")
        if scale < 0:
            raise ValueError("replay timing scale must be >= 0")
        self.plan = plan
        self.timing = timing
        self.scale = scale if timing == "scale" else (0.0 if timing == "none" else 1.0)
        self._lock = threading.Lock()
        self._turn_index = 0
        self._requests = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    @property
    def requests(self) -> int:
        with self._lock:
            return self._requests

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="clawtune-replay-llm", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _next_turn(self) -> ReplayModelTurn:
        with self._lock:
            self._requests += 1
            if self._turn_index >= len(self.plan.turns):
                raise ReplayTraceError("OpenClaw requested more model turns than the source trace contains")
            turn = self.plan.turns[self._turn_index]
            self._turn_index += 1
        delay = turn.duration_ns / 1_000_000_000 * self.scale
        if delay > 0:
            time.sleep(delay)
        return turn

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ClawTuneReplay/1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") == "/v1/models":
                    payload = {"object": "list", "data": [{"id": owner.plan.model, "object": "model", "owned_by": "clawtune-replay"}]}
                    self._send_json(200, payload)
                    return
                self._send_json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self._send_json(404, {"error": {"message": "not found"}})
                    return
                try:
                    length = int(self.headers.get("content-length", "0"))
                    request_payload = json.loads(self.rfile.read(length) or b"{}")
                    turn = owner._next_turn()
                    if bool(request_payload.get("stream")):
                        self._send_stream(turn)
                    else:
                        self._send_json(200, _completion_payload(turn))
                except ReplayTraceError as exc:
                    self._send_json(409, {"error": {"message": str(exc), "type": "replay_exhausted"}})
                except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                    self._send_json(500, {"error": {"message": str(exc), "type": "replay_server_error"}})

            def _send_stream(self, turn: ReplayModelTurn) -> None:
                payload = _completion_payload(turn)
                message = payload["choices"][0]["message"]
                delta: dict[str, Any] = {"role": "assistant"}
                if message.get("content") is not None:
                    delta["content"] = message["content"]
                if message.get("tool_calls"):
                    delta["tool_calls"] = message["tool_calls"]
                chunk = {"id": f"replay-{turn.span_id}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                finish = {"id": f"replay-{turn.span_id}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}
                self.wfile.write(f"data: {json.dumps(finish)}\n\ndata: [DONE]\n\n".encode())

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


def _completion_payload(turn: ReplayModelTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.content if isinstance(turn.content, (str, list, dict)) else str(turn.content)}
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.span_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in turn.tool_calls
        ]
    return {
        "id": f"replay-{turn.span_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": turn.model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if turn.tool_calls else "stop"}],
    }


def write_replay_manifest(
    path: Path,
    *,
    plan: ReplayPlan,
    task_id: str,
    image: str,
    timing: str,
    scale: float,
    result: dict[str, Any] | None = None,
    requests: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema": "clawtune_swe_rebench_replay_v1",
        "task_id": task_id,
        "image": image,
        "source_trace_paths": [str(item) for item in plan.source_paths],
        "source_trace_sha256": plan.source_sha256,
        "source_trace_id": plan.trace_id,
        "timing": {"mode": timing, "scale": scale},
        "model_turn_count": len(plan.turns),
        "tool_count": plan.tool_count,
        "incomplete_source_tool_count": plan.incomplete_tool_count,
        "mock_llm_requests": requests,
        "result": result,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
