"""Failure receipts must not acknowledge lost work or replay partial learning."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from test_tool_resource_predictor import _runtime_sample, _tool_completion


@pytest.mark.parametrize("stage", ["learning", "trace_record", "trace_flush"])
def test_failed_completion_remains_visible_without_replaying_mutations(tmp_path, monkeypatch, stage):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces", tool_resource_ebpf_enabled=False))
    event = _tool_completion("done", "call").model_copy(update={"gateway_id": "g", "runtime_id": "r"})
    sample = _runtime_sample("done", "call")
    counts = {"sample": 0, "learn": 0, "trace": 0}
    def complete(*args, **kwargs):
        counts["sample"] += 1
        return sample
    def learn(*args):
        counts["learn"] += 1  # models a mutation before the exception
        if stage == "learning":
            raise OSError("partially applied learning")
        return 1
    original_record = state.trace_writer.record_tool
    def record(*args):
        counts["trace"] += 1
        original_record(*args)  # may already have queued records when it fails
        if stage == "trace_record":
            raise OSError("partially queued trace")
    monkeypatch.setattr(state.tool_monitor, "complete", complete)
    monkeypatch.setattr(state.predictor, "observe_completion", learn)
    monkeypatch.setattr(state.trace_writer, "record_tool", record)
    original_flush = state.trace_writer.flush
    if stage == "trace_flush":
        monkeypatch.setattr(state.trace_writer, "flush", lambda *args: False)
    with pytest.raises(RuntimeError, match="tool completion processing incomplete"):
        with TestClient(create_app(state)) as client:
            first = client.post("/v1/events/tool-completed", json=event.model_dump(mode="json"))
            retry = client.post("/v1/events/tool-completed", json=event.model_dump(mode="json"))
            assert first.status_code == retry.status_code == 503
            assert first.json() == retry.json()
            assert counts == {"sample": 1, "learn": 1, "trace": 1}
            assert not state._completed_tool_event_ids
            # A successful writer flush cannot erase an uncommitted completion.
            monkeypatch.setattr(state.trace_writer, "flush", original_flush)
            own = client.post("/v1/gateways/g/runtimes/r/drain?timeout_seconds=0&flush_kb=false")
            assert own.json()["drained"] is False
            other = client.post("/v1/gateways/other/runtimes/r/drain?timeout_seconds=0&flush_kb=false")
            assert other.json()["drained"] is True
            final = client.post("/v1/runtime/final-barrier/drain?timeout_seconds=0")
            assert final.json()["kb_flushed"] is False
            # Teardown must not erase the failure receipt before the final barrier.
            client.delete("/v1/gateways/g/runtimes/r/sandbox-scope")
            assert client.post("/v1/runtime/final-barrier/drain?timeout_seconds=0").json()["drained"] is False
    assert state.predictor._closed
    assert not state.trace_writer._writer_thread.is_alive()


def test_concurrent_retry_does_not_acknowledge_inflight_completion(tmp_path, monkeypatch):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces", tool_resource_ebpf_enabled=False))
    event = _tool_completion("done", "call").model_dump(mode="json")
    started, release = Event(), Event()
    calls = []
    monkeypatch.setattr(state.tool_monitor, "complete", lambda *a, **k: _runtime_sample("done", "call"))
    def learn(*args):
        calls.append(1)
        started.set()
        assert release.wait(10)
        return 1
    monkeypatch.setattr(state.predictor, "observe_completion", learn)
    with TestClient(create_app(state)) as client, ThreadPoolExecutor() as pool:
        first = pool.submit(client.post, "/v1/events/tool-completed", json=event)
        try:
            assert started.wait(10)
            pending = client.post("/v1/events/tool-completed", json=event)
            assert pending.status_code == 503
            assert pending.json()["detail"] == "tool_completion_in_progress"
        finally:
            release.set()
        assert first.result(timeout=10).json() == {"stored": True}
        assert client.post("/v1/events/tool-completed", json=event).json() == {"stored": False}
        assert calls == [1]


def test_closed_runtime_rejection_does_not_leave_an_inflight_receipt(tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces", tool_resource_ebpf_enabled=False))
    state.executions.close_runtime("g", "r")
    event = _tool_completion("done", "call").model_copy(update={"gateway_id": "g", "runtime_id": "r"})
    with TestClient(create_app(state)) as client:
        for _ in range(2):
            response = client.post("/v1/events/tool-completed", json=event.model_dump(mode="json"))
            assert response.status_code == 409
            assert response.json()["detail"] == "runtime_finalized"
