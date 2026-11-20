"""Scheduling facts, freshness, restart recovery and independent delivery."""
import asyncio
import time
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError

from clawtune_kb.contracts import validate
from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.session_state import SessionStateManager
from clawtune_sidecar.signal_protocol import envelope
from clawtune_sidecar.signals import SignalHub


def observation(*, revision=1, run="run", session="session", tools=("a", "b"),
                settled=(), foreground=True, status="active", model=None, epoch=1):
    return envelope("session.observation", {
        "revision": revision, "observer_id": f"observer-{epoch}", "observer_started_at": epoch,
        "session_key": "key-" + session, "parent_session_key": None,
        "run_status": status, "model_call_id": model,
        "batch": {"batch_id": run + ":batch", "tools": [
            {"tool_call_id": t, "settled": t in settled, "foreground": foreground}
            for t in tools]} if tools else None,
    }, source="plugin", identity={"gateway_id": "gateway", "runtime_id": "runtime",
                                  "session_id": session, "run_id": run})


def setup():
    hub = SignalHub()
    now = [100.0]
    hub.sessions = SessionStateManager(hub.publish, clock=lambda: now[0])
    return hub, hub.sessions, now


def progress(manager, message, tool, now, remaining=1500, validity=1):
    owner = {**message["identity"], "tool_call_id": tool}
    manager.progress(owner, {"remaining_duration_ms": remaining}, now, now + validity)


def latest(manager):
    return manager.snapshots()[-1]


def test_parallel_requires_all_members_and_completion_is_logical():
    hub, manager, now = setup()
    message = observation()
    manager.observe(message)
    progress(manager, message, "a", now[0])
    assert latest(manager)["payload"]["remaining_ms"] is None
    progress(manager, message, "b", now[0], remaining=1800)
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1800)
    manager.process_exited({**message["identity"], "tool_call_id": "a"})
    assert latest(manager)["payload"]["phase"] == "TOOLS"
    assert latest(manager)["payload"]["remaining_ms"] is None
    manager.observe(observation(revision=2, settled=("a",)))
    assert latest(manager)["payload"]["phase"] == "TOOLS"
    manager.observe(observation(revision=3, settled=("a", "b")))
    assert latest(manager)["payload"]["phase"] == "HANDOFF"
    for m in hub.recent:
        validate(m, "signal.schema.json")


def test_serial_pending_unknown_background_and_expiry():
    _, manager, now = setup()
    message = observation()
    manager.observe(message)
    progress(manager, message, "a", now[0])
    assert latest(manager)["payload"]["remaining_ms"] is None  # b has not started
    manager.observe(observation(revision=2, settled=("a",)))
    progress(manager, message, "b", now[0], remaining=500)
    assert latest(manager)["payload"]["remaining_ms"] == 500
    now[0] += 1.1
    assert latest(manager)["payload"]["remaining_ms"] is None
    manager.observe(observation(revision=3, foreground=False))
    progress(manager, message, "a", now[0])
    progress(manager, message, "b", now[0])
    assert latest(manager)["payload"]["remaining_ms"] is None
    manager.observe(observation(revision=4, foreground=False, settled=("a", "b")))
    assert latest(manager)["payload"]["phase"] == "HANDOFF"  # background handle returned


def test_foreground_retraction_and_stale_heartbeat():
    _, manager, now = setup()
    message = observation(tools=("a",))
    manager.observe(message)
    progress(manager, message, "a", now[0])
    assert latest(manager)["payload"]["remaining_ms"] == 1500
    manager.observe(observation(revision=2, tools=("a",), foreground=False))
    assert latest(manager)["payload"]["remaining_ms"] is None
    now[0] += 16
    stale = observation(revision=3, status="ended")
    stale["observed_at"] = 100
    assert not manager.observe(stale)
    assert latest(manager)["payload"]["phase"] == "UNKNOWN"


def test_session_only_delivery_keeps_internal_tool_observations(monkeypatch):
    monkeypatch.setenv("CLAWTUNE_SIGNAL_TOOL_EVENTS", "false")
    assert SidecarConfig.from_env().signal_tool_events is False
    hub = SignalHub("http://receiver", tool_events=False)
    hub.publish("model.started", {})
    hub.sessions.observe(observation())
    assert hub.queue.empty()
    assert hub.task is None
    assert len(hub.session_pending) == 1
    assert [m["type"] for m in hub.recent] == ["model.started", "session.state"]


def test_raw_progress_refreshes_session_after_one_shot_tool_notification():
    hub = SignalHub()
    snapshot = observation(tools=("a",))
    hub.sessions.observe(snapshot)
    owner = {**snapshot["identity"], "tool_call_id": "a", "execution_id": "exec"}
    hub.register(SimpleNamespace(**owner))
    hub.observe(envelope("execution.started", {"pid": 123, "progress_adapter": "pytest"},
                         source="launcher", identity=owner))
    now = time.time() - .01
    for index, remaining in enumerate((1500, 1800)):
        observed = now + .001 * index
        assert hub.observe(envelope("execution.progress", {
            "elapsed_duration_ms": 500 + index, "remaining_duration_ms": remaining,
            "remaining_source": "pytest.completed_percent", "remaining_kind": "estimated",
        }, source="launcher", identity=owner, observed_at=observed,
            expires_at=observed + 1))
    assert sum(m["type"] == "execution.finishing_soon" for m in hub.recent) == 1
    forecasts = [m for m in hub.recent if m["type"] == "session.handoff_forecast"]
    assert len(forecasts) == 2
    assert forecasts[-1]["payload"]["remaining_ms"] > forecasts[0]["payload"]["remaining_ms"]
    # Reused call IDs must not attach an old execution to a later assistant batch.
    next_batch = observation(revision=2, tools=("a",))
    next_batch["payload"]["batch"]["batch_id"] = "run:next-batch"
    hub.sessions.observe(next_batch)
    assert hub.observe(envelope("execution.progress", {
        "elapsed_duration_ms": 600, "remaining_duration_ms": 1800,
        "remaining_source": "pytest.completed_percent", "remaining_kind": "estimated",
    }, source="launcher", identity=owner, observed_at=now + .002, expires_at=now + 1))
    assert latest(hub.sessions)["payload"]["remaining_ms"] is None


@pytest.mark.parametrize("status,phase", [("yielded", "WAIT_EVENT"), ("cancelled", "IDLE"),
                                         ("error", "IDLE"), ("finishing", "CONTROL")])
def test_stop_never_manufactures_handoff(status, phase):
    hub, manager, _ = setup()
    manager.observe(observation())
    manager.observe(observation(revision=2, status=status))
    assert latest(manager)["payload"]["phase"] == phase
    assert all(m["payload"]["phase"] != "HANDOFF" for m in hub.recent)


def test_revisions_epochs_isolation_and_recovery():
    hub, manager, now = setup()
    first = observation()
    manager.observe(first)
    manager.observe(observation(revision=3, run="next", tools=(), model="model-2"))
    assert not manager.observe(observation(revision=2))
    progress(manager, first, "a", now[0])
    assert latest(manager)["payload"]["phase"] == "LLM"
    manager.observe(observation(session="another", status="yielded"))
    assert len(manager.snapshots()) == 2
    restored = observation(revision=1, epoch=2, status="ended")
    assert manager.observe(restored)
    assert not manager.observe(first)  # old plugin instance
    now[0] += 16
    assert all(m["payload"]["phase"] == "UNKNOWN" for m in manager.snapshots())
    assert not manager.observe(restored)  # heartbeat restores liveness without a new revision
    assert manager.snapshots()[0]["payload"]["phase"] == "IDLE"
    restarted = SessionStateManager(hub.publish, clock=lambda: now[0])
    assert restarted.snapshots() == []  # absence is unknown, not IDLE
    restarted.observe(restored)
    assert latest(restarted)["payload"]["phase"] == "IDLE"


def test_conflicting_revision_duplicate_members_and_protocol_direction():
    _, manager, _ = setup()
    message = observation()
    manager.observe(message)
    changed = deepcopy(message)
    changed["payload"]["run_status"] = "ended"
    with pytest.raises(ValueError, match="revision_conflict"):
        manager.observe(changed)
    with pytest.raises(ValueError, match="duplicate_batch_member"):
        manager.observe(observation(revision=2, tools=("a", "a")))
    validate(message, "signal.schema.json")
    message["source"] = "launcher"
    with pytest.raises(ValidationError):
        validate(message, "signal.schema.json")


def test_api_auth_snapshots_and_sidecar_restart(tmp_path):
    def app():
        return create_app(build_state(SidecarConfig(trace_dir=tmp_path / "traces",
            auth_token="secret", tool_resource_ebpf_enabled=False, pmu_enabled=False)))
    message = observation()
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app()) as client:
        assert client.post("/v1/signals", json=message).status_code == 401
        assert client.get("/v1/sessions").status_code == 401
        assert client.post("/v1/signals", json=message, headers=headers).json() == {"stored": True}
        snapshot = client.get("/v1/sessions", headers=headers).json()
        validate(snapshot["sessions"][0], "signal.schema.json")
    with TestClient(app()) as client:
        assert client.get("/v1/sessions", headers=headers).json()["sessions"] == []
        assert client.post("/v1/signals", json=message, headers=headers).json() == {"stored": True}
        restored = client.get("/v1/sessions", headers=headers).json()
        assert snapshot["publisher_id"] != restored["publisher_id"]
        assert restored["sessions"][0]["payload"]["phase"] == "TOOLS"


def test_session_delivery_bypasses_blocked_legacy_and_resyncs_latest():
    async def run():
        legacy_started, session_delivered = asyncio.Event(), asyncio.Event()
        received = []
        async def handler(request):
            import json
            message = json.loads(request.content)
            if not message["type"].startswith("session."):
                legacy_started.set()
                await asyncio.Event().wait()
            received.append(message)
            session_delivered.set()
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            hub = SignalHub("http://receiver", client=client, timeout=10)
            hub.publish("model.started", {})
            await asyncio.wait_for(legacy_started.wait(), .5)
            hub.sessions.observe(observation())
            hub.sessions.observe(observation(revision=2, status="yielded"))
            hub.start_sessions()
            try:
                await asyncio.wait_for(session_delivered.wait(), .5)
                assert len(received) == 1
                assert received[0]["payload"]["phase"] == "WAIT_EVENT"
                session_delivered.clear()
                await asyncio.wait_for(session_delivered.wait(), 6)
                assert received[-1]["signal_id"] == received[0]["signal_id"]
            finally:
                await hub.close()
    asyncio.run(run())
