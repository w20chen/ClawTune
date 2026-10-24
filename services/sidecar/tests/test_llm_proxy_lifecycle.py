import asyncio
import json

import httpx
import pytest

from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.llm_proxy import _stream_chat


class _Writer:
    def __init__(self, *, fail_record=False):
        self.active = 1
        self.calls = []
        self.fail_record = fail_record

    def record_llm_proxy_call(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_record:
            raise OSError("synthetic trace storage failure")

    def end_proxy_activity(self, runtime_id):
        assert runtime_id == "synthetic-runtime"
        self.active -= 1


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, hang=False):
        self.chunks = chunks
        self.hang = hang
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.hang:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def _response(monkeypatch, tmp_path, stream, writer):
    real_client = httpx.AsyncClient

    async def upstream(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(upstream), **kwargs,
    ))
    return _stream_chat("https://synthetic.invalid/v1/chat/completions",
                        headers={}, body=b"{}", payload={"model": "synthetic"},
                        trace_writer=writer, runtime_id="synthetic-runtime",
                        action_id="synthetic-call", started_at=0,
                        config=SidecarConfig(trace_dir=tmp_path))


@pytest.mark.parametrize("partial", [False, True])
def test_cancelled_stream_closes_upstream_and_records_incomplete(monkeypatch, tmp_path, partial):
    async def run():
        chunks = [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'] if partial else []
        upstream = _Stream(chunks, hang=True)
        writer = _Writer()
        response = _response(monkeypatch, tmp_path, upstream, writer)
        if partial:
            assert b"partial" in await anext(response)
        waiting = asyncio.create_task(anext(response))
        await asyncio.wait_for(upstream.waiting.wait(), timeout=2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert upstream.closed
        assert writer.active == 0
        assert writer.calls[-1]["error"] == "stream_cancelled"
        assert writer.calls[-1]["status_code"] == 499

    asyncio.run(run())


def test_closing_partial_iterator_is_not_a_successful_trace(monkeypatch, tmp_path):
    async def run():
        upstream = _Stream([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'], hang=True)
        writer = _Writer()
        response = _response(monkeypatch, tmp_path, upstream, writer)
        await anext(response)
        await response.aclose()
        assert upstream.closed and writer.active == 0
        assert writer.calls[-1]["error"] == "stream_cancelled"

    asyncio.run(run())


def test_trace_write_failure_still_releases_stream_activity(monkeypatch, tmp_path):
    async def run():
        upstream = _Stream([b'data: {"choices":[{"delta":{"content":"complete"}}]}\n\ndata: [DONE]\n\n'])
        writer = _Writer(fail_record=True)
        response = _response(monkeypatch, tmp_path, upstream, writer)
        with pytest.raises(OSError, match="synthetic trace storage"):
            _ = [chunk async for chunk in response]
        assert upstream.closed and writer.active == 0

    asyncio.run(run())


def test_length_finish_and_close_after_done_remain_complete(monkeypatch, tmp_path):
    async def run():
        event = {"choices": [{"delta": {"content": "x" * 8192}, "finish_reason": "length"}]}
        upstream = _Stream([("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()])
        writer = _Writer()
        response = _response(monkeypatch, tmp_path, upstream, writer)
        assert b"length" in await anext(response)
        assert await anext(response) == b"data: [DONE]\n\n"
        await response.aclose()
        assert writer.active == 0 and upstream.closed
        assert writer.calls[-1]["error"] is None
        assert writer.calls[-1]["raw_response"]["message"]["finish_reason"] == "length"
        assert len(writer.calls[-1]["content"]) == 8192

    asyncio.run(run())
