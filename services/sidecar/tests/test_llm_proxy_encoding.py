from __future__ import annotations

import gzip
import json
import zlib

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.llm_proxy import proxy_chat_completions, proxy_models


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        # Split both compression framing and SSE records across network chunks.
        for offset in range(0, len(self.body), 7):
            yield self.body[offset:offset + 7]


def _client(monkeypatch, tmp_path, body, encoding, *, status=200, media_type="application/json"):
    wire_body = {"gzip": gzip.compress, "deflate": zlib.compress,
                 "identity": lambda value: value}[encoding](body)
    requests = []

    async def upstream(request):
        requests.append(request)
        return httpx.Response(status, headers={
            "content-type": media_type,
            "content-encoding": encoding,
            "content-length": str(len(wire_body)),
            "content-md5": "stale-md5",
            "digest": "sha-256=stale",
            "content-digest": "sha-256=:stale:",
            "repr-digest": "sha-256=:stale:",
            "etag": '"upstream-representation"',
            "connection": "keep-alive, x-upstream-connection",
            "x-upstream-connection": "hop-only",
            "x-request-id": "synthetic-request-id",
            "cache-control": "no-store",
        }, stream=_Chunks(wire_body))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(upstream), **kwargs,
    ))
    config = SidecarConfig(trace_dir=tmp_path, llm_proxy_upstream_base_url="https://synthetic.invalid")
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await proxy_chat_completions(request, config, None)

    @app.get("/v1/models")
    async def models(request: Request):
        return await proxy_models(request, config)

    return TestClient(app), requests


def _assert_rewritten_headers(response):
    for header in ("content-encoding", "content-md5", "digest", "content-digest",
                   "repr-digest", "etag", "connection", "x-upstream-connection",
                   "transfer-encoding"):
        assert header not in response.headers
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["x-request-id"] == "synthetic-request-id"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity"])
def test_nonstream_chat_decodes_and_reserializes_without_stale_encoding(monkeypatch, tmp_path, encoding):
    payload = {"choices": [{"message": {"role": "assistant", "content": "",
                                        "reasoning_content": "synthetic reasoning"}}]}
    body = json.dumps(payload, indent=4).encode()
    client, requests = _client(monkeypatch, tmp_path, body, encoding)
    with client:
        response = client.post("/v1/chat/completions", json={"model": "synthetic", "messages": []},
                               headers={"authorization": "Bearer synthetic-test-key"})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "synthetic reasoning"
    assert response.content != body  # reasoning merge and JSON serialization both apply
    assert requests[0].headers["authorization"] == "Bearer synthetic-test-key"
    assert response.headers["content-type"] == "application/json"
    _assert_rewritten_headers(response)


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("path", ["/v1/models", "/v1/chat/completions"])
def test_decoded_plain_error_fallback_also_removes_encoding(monkeypatch, tmp_path, encoding, path):
    body = b"synthetic upstream unavailable"
    client, _ = _client(monkeypatch, tmp_path, body, encoding, status=503, media_type="text/plain")
    with client:
        response = client.get(path) if path.endswith("models") else client.post(path, json={})
    assert response.status_code == 503
    assert response.content == body
    assert response.headers["content-type"] == "text/plain"
    _assert_rewritten_headers(response)


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_models_discovery_normalizes_decoded_json(monkeypatch, tmp_path, encoding):
    body = json.dumps({"object": "list", "data": [
        {"id": "synthetic-model", "object": "model", "owned_by": "other-provider"},
    ]}).encode()
    client, _ = _client(monkeypatch, tmp_path, body, encoding)
    with client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "synthetic-model"
    assert response.json()["data"][0]["owned_by"] == "organization"
    assert "content-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(response.content)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity"])
def test_stream_decodes_chunks_without_forwarding_upstream_representation_headers(
    monkeypatch, tmp_path, encoding,
):
    event = {"choices": [{"delta": {"role": "assistant", "reasoning_content": "synthetic stream"}}]}
    body = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
    client, _ = _client(monkeypatch, tmp_path, body, encoding, media_type="text/event-stream")
    with client:
        response = client.post("/v1/chat/completions", json={"stream": True, "messages": []})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in response.headers
    assert "content-length" not in response.headers
    assert "etag" not in response.headers
    events = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    assert len(events) == 2
    assert json.loads(events[0])["choices"][0]["delta"]["content"] == "synthetic stream"
