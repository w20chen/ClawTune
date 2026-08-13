from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any
from uuid import uuid4

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.trace import AgentTestBenchTraceWriter


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
_RUNTIME_API_KEY_PREFIX = "clawtune-runtime."


async def proxy_models(request: Request, config: SidecarConfig) -> Response:
    # When expose_model is explicitly set, return a synthetic model list
    # containing only that model ID (useful for model-name translation).
    if config.llm_proxy_expose_model:
        return _synthetic_models_response(config.llm_proxy_expose_model)

    upstream = _upstream_url(config, request.url.path)
    if upstream is None:
        return _not_configured()
    headers = _forward_headers(request, config)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request("GET", upstream, headers=headers, params=request.query_params)

    # Default behaviour: always normalise the upstream /v1/models response
    # so OpenClaw provider discovery does not reject unfamiliar metadata
    # (e.g. DeepSeek's "owned_by":"deepseek" causes "provider mismatch").
    # When normalisation succeeds the response is transparent to callers
    # except for the sanitised metadata fields.
    normalized = _normalize_models_response(response.content)
    if normalized is not None:
        return JSONResponse(content=normalized, status_code=response.status_code)

    # Fallback: return the raw upstream response as-is.
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=_response_headers(response),
        media_type=response.headers.get("content-type"),
    )


async def proxy_chat_completions(
    request: Request,
    config: SidecarConfig,
    trace_writer: AgentTestBenchTraceWriter | None,
) -> Response:
    upstream = _upstream_url(config, request.url.path)
    if upstream is None:
        return _not_configured()

    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        payload = {}

    # Translate model name: OpenClaw sends the exposed model ID; the
    # upstream provider expects the real model ID.
    _translate_model(payload, config)
    body = json.dumps(payload).encode("utf-8") if payload else body

    stream = bool(payload.get("stream"))
    runtime_id = _runtime_id_from_request(request)
    started_at = time.time()
    action_id = f"llm-proxy-{uuid4()}"
    headers = _forward_headers(request, config)

    if stream:
        if trace_writer is not None:
            trace_writer.begin_proxy_activity(runtime_id)
        return StreamingResponse(
            _stream_chat(
                upstream,
                headers=headers,
                body=body,
                payload=payload,
                trace_writer=trace_writer,
                runtime_id=runtime_id,
                action_id=action_id,
                started_at=started_at,
                config=config,
            ),
            media_type="text/event-stream",
        )

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            response = await client.post(upstream, headers=headers, content=body)
        except Exception as exc:
            _record_proxy_trace(
                trace_writer,
                runtime_id=runtime_id,
                action_id=action_id,
                payload=payload,
                response_payload=None,
                started_at=started_at,
                status_code=502,
                stream=False,
                error=str(exc),
            )
            return JSONResponse({"error": {"message": str(exc), "type": "proxy_error"}}, status_code=502)

    response_payload = _json_or_text(response.content)
    # Merge reasoning_content → content for reasoning models (deepseek-v4-flash, etc.)
    # so OpenClaw sees readable text instead of empty content.
    _merge_reasoning(response_payload)
    response_message = _message_from_response(response_payload)
    if (
        response.status_code < 400
        and not _assistant_message_has_payload(response_message)
    ):
        error = "upstream_empty_response"
        _write_proxy_debug(
            config,
            action_id=action_id,
            upstream=upstream,
            payload=payload,
            status_code=response.status_code,
            chunk_count=0,
            message=response_message or {"role": "assistant", "content": ""},
            raw_preview=response.content,
            error=error,
        )
        _record_proxy_trace(
            trace_writer,
            runtime_id=runtime_id,
            action_id=action_id,
            payload=payload,
            response_payload=response_payload,
            started_at=started_at,
            status_code=502,
            stream=False,
            error=error,
        )
        return JSONResponse(
            {
                "error": {
                    "message": "Upstream model returned no content or tool calls",
                    "type": error,
                }
            },
            status_code=502,
        )
    response_content = json.dumps(response_payload).encode("utf-8") if isinstance(response_payload, dict) else response.content
    _record_proxy_trace(
        trace_writer,
        runtime_id=runtime_id,
        action_id=action_id,
        payload=payload,
        response_payload=response_payload,
        started_at=started_at,
        status_code=response.status_code,
        stream=False,
        error=None if response.status_code < 400 else f"upstream_http_{response.status_code}",
    )
    return Response(
        content=response_content,
        status_code=response.status_code,
        headers=_response_headers(response),
        media_type=response.headers.get("content-type"),
    )


async def _stream_chat(
    upstream: str,
    *,
    headers: dict[str, str],
    body: bytes,
    payload: dict[str, Any],
    trace_writer: AgentTestBenchTraceWriter | None,
    runtime_id: str | None,
    action_id: str,
    started_at: float,
    config: SidecarConfig,
):
    chunks: list[dict[str, Any]] = []
    status_code = 200
    error: str | None = None
    raw_preview = bytearray()
    # Buffer for SSE data that may span across HTTP chunk boundaries.
    # httpx's aiter_bytes() does not guarantee SSE-event-aligned chunks.
    sse_buffer = b""
    done_seen = False
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", upstream, headers=headers, content=body) as response:
                status_code = response.status_code
                async for chunk in response.aiter_bytes():
                    _append_preview(raw_preview, chunk)
                    sse_buffer += chunk
                    # Extract complete SSE events (delimited by double-newline).
                    events, sse_buffer = _parse_sse_buffer(sse_buffer)
                    for event in events:
                        if event is None:
                            done_seen = True
                        else:
                            # Merge reasoning_content → content for reasoning models.
                            _merge_reasoning(event)
                            chunks.append(event)
                            if isinstance(event.get("error"), dict):
                                error = str(
                                    event["error"].get("type")
                                    or "upstream_stream_error"
                                )
                    events = [event for event in events if event is not None]
                    if events:
                        # Only forward complete SSE events. Forwarding raw
                        # partial chunks corrupts JSON when the event is later
                        # re-serialized after its remaining bytes arrive.
                        yield _serialize_sse(events)
        # Flush any remaining partial event after the stream ends.
        if sse_buffer:
            events, _ = _parse_sse_buffer(sse_buffer + b"\n\n")
            for event in events:
                if event is None:
                    done_seen = True
                else:
                    _merge_reasoning(event)
                    chunks.append(event)
                    if isinstance(event.get("error"), dict):
                        error = str(
                            event["error"].get("type")
                            or "upstream_stream_error"
                        )
            events = [event for event in events if event is not None]
            if events:
                yield _serialize_sse(events)
    except Exception as exc:
        status_code = 502
        error = str(exc)
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'proxy_error'}})}\n\n".encode(
            "utf-8"
        )
    else:
        message = _message_from_stream_chunks(chunks)
        if error is None and status_code >= 400:
            error = f"upstream_http_{status_code}"
            yield _serialize_sse(
                [
                    {
                        "error": {
                            "message": f"Upstream model returned HTTP {status_code}",
                            "type": error,
                        }
                    },
                    None,
                ]
            )
            done_seen = False
        elif (
            error is None
            and status_code < 400
            and not _assistant_message_has_payload(message)
        ):
            error = "upstream_empty_response"
            yield _serialize_sse(
                [
                    {
                        "error": {
                            "message": "Upstream model returned no content or tool calls",
                            "type": error,
                        }
                    },
                    None,
                ]
            )
            done_seen = False
        if error is None and done_seen:
            yield _serialize_sse([None])
    finally:
        message = _message_from_stream_chunks(chunks)
        effective_error = error if error else (None if status_code < 400 else f"upstream_http_{status_code}")
        _write_proxy_debug(
            config,
            action_id=action_id,
            upstream=upstream,
            payload=payload,
            status_code=status_code,
            chunk_count=len(chunks),
            message=message,
            raw_preview=bytes(raw_preview),
            error=effective_error,
        )
        _record_proxy_trace(
            trace_writer,
            runtime_id=runtime_id,
            action_id=action_id,
            payload=payload,
            response_payload={"message": message},
            started_at=started_at,
            status_code=status_code,
            stream=True,
            error=effective_error,
        )
        if trace_writer is not None:
            trace_writer.end_proxy_activity(runtime_id)


def _record_proxy_trace(
    trace_writer: AgentTestBenchTraceWriter | None,
    *,
    runtime_id: str | None,
    action_id: str,
    payload: dict[str, Any],
    response_payload: Any | None,
    started_at: float,
    status_code: int,
    stream: bool,
    error: str | None,
) -> None:
    if trace_writer is None:
        return
    trace_writer.record_llm_proxy_call(
        runtime_id=runtime_id,
        action_id=action_id,
        provider="llm-proxy",
        model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        messages_in=payload.get("messages"),
        content=_content_from_response(response_payload),
        raw_request=payload,
        raw_response=response_payload,
        ts_start=started_at,
        ts_end=time.time(),
        status_code=status_code,
        stream=stream,
        error=error,
    )


def _runtime_id_from_request(request: Request) -> str | None:
    explicit = request.headers.get("x-clawtune-runtime-id", "").strip()
    if explicit:
        return explicit[:128]
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token.startswith(_RUNTIME_API_KEY_PREFIX):
        return None
    runtime_id = token[len(_RUNTIME_API_KEY_PREFIX) :].strip()
    return runtime_id[:128] or None


def _upstream_url(config: SidecarConfig, path: str) -> str | None:
    if not config.llm_proxy_enabled or not config.llm_proxy_upstream_base_url:
        return None
    base = config.llm_proxy_upstream_base_url.rstrip("/")
    if path.startswith("/v1/") and base.endswith("/v1"):
        suffix = path[len("/v1") :]
    else:
        suffix = path
    return base + suffix


def _append_preview(buffer: bytearray, chunk: bytes, limit: int = 8192) -> None:
    remaining = limit - len(buffer)
    if remaining <= 0:
        return
    buffer.extend(chunk[:remaining])


def _write_proxy_debug(
    config: SidecarConfig,
    *,
    action_id: str,
    upstream: str,
    payload: dict[str, Any],
    status_code: int,
    chunk_count: int,
    message: dict[str, Any],
    raw_preview: bytes,
    error: str | None,
) -> None:
    automatic_empty_diagnostic = error == "upstream_empty_response"
    if not config.llm_proxy_debug_dump and not automatic_empty_diagnostic:
        return
    try:
        config.trace_dir.mkdir(parents=True, exist_ok=True)
        path = config.trace_dir / f"llm_proxy_debug_{action_id}.json"
        body = {
            "action_id": action_id,
            "upstream": upstream,
            "request_model": payload.get("model"),
            "request_stream": payload.get("stream"),
            "status_code": status_code,
            "chunk_count": chunk_count,
            "message_content_bytes": len(str(message.get("content") or "").encode("utf-8")),
            "has_tool_calls": bool(message.get("tool_calls")),
            "finish_reason": message.get("finish_reason"),
            "error": error,
            "raw_preview_bytes": len(raw_preview),
            "raw_preview_sha256": hashlib.sha256(raw_preview).hexdigest(),
            "automatic_empty_diagnostic": automatic_empty_diagnostic,
        }
        if config.llm_proxy_debug_dump:
            body["raw_preview"] = raw_preview.decode("utf-8", errors="replace")
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return


def _translate_model(payload: dict[str, Any], config: SidecarConfig) -> None:
    """Rewrite the model field from exposed→upstream when model spoofing is active."""
    expose = config.llm_proxy_expose_model
    upstream = config.llm_proxy_upstream_model or expose
    if not expose or not upstream or expose == upstream:
        return
    if isinstance(payload.get("model"), str) and payload["model"] == expose:
        payload["model"] = upstream


def _merge_reasoning(body: dict[str, Any] | None) -> None:
    """Merge ``reasoning_content`` into ``content`` for reasoning models.

    Reasoning models (deepseek-v4-flash, o1, etc.) output thinking text in
    ``reasoning_content`` and leave ``content`` empty.  OpenClaw only reads
    ``content``, so we copy the reasoning text there before forwarding.
    """
    if not isinstance(body, dict):
        return
    for choice in body.get("choices", []):
        if not isinstance(choice, dict):
            continue
        # Non-streaming: message.content
        msg = choice.get("message")
        if isinstance(msg, dict):
            rc = msg.get("reasoning_content")
            if rc and not msg.get("content"):
                msg["content"] = rc
        # Streaming: delta.content
        delta = choice.get("delta")
        if isinstance(delta, dict):
            rc = delta.get("reasoning_content")
            if rc and not delta.get("content"):
                delta["content"] = rc


def _serialize_sse(events: list[dict[str, Any] | None]) -> bytes:
    """Re-serialize modified SSE events, preserving [DONE] markers."""
    parts: list[bytes] = []
    for event in events:
        if event is None:
            parts.append(b"data: [DONE]\n\n")
        else:
            parts.append(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
    return b"".join(parts)


def _normalize_models_response(raw: bytes) -> dict[str, Any] | None:
    """Rewrite upstream /v1/models metadata to be OpenClaw-compatible.

    Upstream providers may return model entries with ``owned_by`` values
    that OpenClaw's provider discovery rejects (e.g. ``"deepseek"`` for
    the vllm provider).  This function rewrites every model entry's
    ``owned_by`` to ``"organization"``, which is accepted by all
    OpenAI-compatible providers.

    Returns the normalised response dict, or ``None`` if the upstream
    response cannot be parsed as a valid model list.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and "owned_by" in entry:
            entry["owned_by"] = "organization"
    return body


def _synthetic_models_response(model_id: str) -> JSONResponse:
    """Return an OpenAI-compatible /v1/models response for a single model.

    The response is shaped to satisfy OpenClaw provider discovery
    (vllm, openai-compatible, etc.) without proxying to the real upstream.
    """
    import time as _time
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": int(_time.time()),
                "owned_by": "organization",
            }
        ],
    })


def _forward_headers(request: Request, config: SidecarConfig) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    # Default plugin mode reuses the API key already configured in OpenClaw:
    # OpenClaw sends it as Authorization, and the proxy forwards it upstream.
    # A sidecar upstream key is an explicit advanced override.
    if config.llm_proxy_upstream_api_key:
        headers["authorization"] = f"Bearer {config.llm_proxy_upstream_api_key}"
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def _not_configured() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "CLAWTUNE_LLM_UPSTREAM_BASE_URL is not configured",
                "type": "llm_proxy_not_configured",
            }
        },
        status_code=502,
    )


def _json_or_text(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


def _parse_sse_buffer(buffer: bytes) -> tuple[list[dict[str, Any] | None], bytes]:
    """Parse complete SSE events from a byte buffer.

    SSE events are delimited by an LF or CRLF blank line. Events that span
    HTTP chunk boundaries stay in the returned remainder buffer.

    Returns (events, remainder) where remainder is the trailing incomplete
    event bytes (may be empty).
    """
    events: list[dict[str, Any] | None] = []
    # Find the last complete LF/CRLF event boundary.
    boundaries = list(re.finditer(rb"\r?\n\r?\n", buffer))
    if not boundaries:
        # No complete event yet — whole buffer is partial, keep it all.
        return events, buffer

    last_delim_end = boundaries[-1].end()
    complete = buffer[:last_delim_end]
    remainder = buffer[last_delim_end:]

    text = complete.decode("utf-8", errors="replace").replace("\r\n", "\n")
    # SSE events may use \n or \r\n; normalize to \n for splitting.
    current_data: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            # Empty line after data lines = end of one SSE event.
            if current_data:
                _flush_sse_event("".join(current_data), events)
                current_data = []
            continue
        if line.startswith("data:"):
            current_data.append(line[len("data:"):].strip())
        # Non-data fields (event:, id:, retry:) are ignored for trace purposes.
    # Flush any remaining data lines (shouldn't happen with proper \n\n delim).
    if current_data:
        _flush_sse_event("".join(current_data), events)

    return events, remainder


def _flush_sse_event(data_str: str, events: list[dict[str, Any] | None]) -> None:
    """Parse a single SSE data payload into an event and append to the list."""
    if not data_str or data_str == "[DONE]":
        events.append(None)
        return
    try:
        events.append(json.loads(data_str))
    except json.JSONDecodeError:
        # Malformed JSON in SSE event — log and skip rather than silently drop.
        import logging
        _log = logging.getLogger(__name__)
        _log.debug("llm_proxy: skipping unparseable SSE data: %.200s", data_str)


def _content_from_response(response_payload: Any | None) -> Any | None:
    """Extract the meaningful output from an LLM API response.

    Returns the text content when present, the tool_calls when the response
    is a tool-call-only turn (content is null/empty), or the full message
    dict as a fallback.
    """
    message = _message_from_response(response_payload)
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    if tool_calls:
        return {"content": content or "", "tool_calls": tool_calls}
    # If there is real text content, return it.
    if content:
        return content
    # Return content even when empty/falsy (preserves the empty string
    # for callers that need to distinguish "no text" from "no response").
    return content


def _message_from_response(response_payload: Any | None) -> dict[str, Any] | None:
    """Extract the assistant message dict from an LLM API response."""
    if response_payload is None:
        return None
    if isinstance(response_payload, dict) and "message" in response_payload:
        msg = response_payload.get("message")
        if isinstance(msg, dict):
            return msg
    if not isinstance(response_payload, dict):
        return None
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    msg = first.get("message")
    if isinstance(msg, dict):
        return msg
    text = first.get("text")
    if isinstance(text, str):
        return {"content": text}
    return None


def _content_from_stream_chunks(chunks: list[dict[str, Any]]) -> Any:
    """Extract meaningful output from streamed LLM response chunks."""
    message = _message_from_stream_chunks(chunks)
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    if tool_calls:
        return {"content": content or "", "tool_calls": tool_calls}
    if content:
        return content
    return content


def _message_from_stream_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    for chunk in chunks:
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if isinstance(choice.get("finish_reason"), str):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                parts.append(delta["content"])
            if isinstance(delta, dict) and isinstance(delta.get("tool_calls"), list):
                _merge_tool_call_deltas(tool_calls, delta["tool_calls"])
            elif isinstance(choice.get("text"), str):
                parts.append(choice["text"])
    message: dict[str, Any] = {"role": "assistant", "content": "".join(parts)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    if finish_reason is not None:
        message["finish_reason"] = finish_reason
    return message


def _assistant_message_has_payload(message: dict[str, Any] | None) -> bool:
    if not isinstance(message, dict):
        return False
    if _assistant_content_has_payload(message.get("content")):
        return True
    tool_calls = message.get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def _assistant_content_has_payload(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_assistant_content_has_payload(item) for item in value)
    if isinstance(value, dict):
        return any(
            _assistant_content_has_payload(value.get(key))
            for key in ("text", "content", "reasoning_content")
            if key in value
        )
    return False


def _merge_tool_call_deltas(
    output: dict[int, dict[str, Any]], deltas: list[Any]
) -> None:
    for item in deltas:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            index = len(output)
        current = output.setdefault(index, {"index": index, "type": "function", "function": {}})
        if isinstance(item.get("id"), str):
            current["id"] = item["id"]
        if isinstance(item.get("type"), str):
            current["type"] = item["type"]
        function = item.get("function")
        if isinstance(function, dict):
            current_function = current.setdefault("function", {})
            if isinstance(function.get("name"), str):
                current_function["name"] = function["name"]
            if isinstance(function.get("arguments"), str):
                current_function["arguments"] = (
                    str(current_function.get("arguments") or "") + function["arguments"]
                )
