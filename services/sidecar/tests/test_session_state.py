"""Scheduling facts, freshness, restart recovery and independent delivery."""
import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError

from clawtune_kb.contracts import validate
from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.contracts.models import ToolPrediction
from clawtune_sidecar.session_state import SessionStateManager
from clawtune_sidecar.signal_protocol import envelope
from clawtune_sidecar.signals import SignalHub


def observation(*, revision=1, run="run", session="session", tools=("a", "b"),
                started=(), settled=(), foreground=True, predicted=1000,
                mode="parallel", mode_source="openclaw_default_assumption",
                status="active", model=None, epoch=1):
    durations = predicted if isinstance(predicted, dict) else {tool: predicted for tool in tools}
    return envelope("session.observation", {
        "revision": revision, "observer_id": f"observer-{epoch}", "observer_started_at": epoch,
        "session_key": "key-" + session, "parent_session_key": None,
        "active_child_session_keys": [], "wait": None,
        "run_status": status, "model_call_id": model,
        "batch": {"batch_id": run + ":batch", "execution_mode": mode,
                  "mode_source": mode_source, "tools": [
            {"tool_call_id": t, "started": t in started or t in settled,
             "settled": t in settled, "foreground": foreground,
             "predicted_duration_ms": durations.get(t),
             "prediction_source": "lattice" if durations.get(t) is not None else None}
            for t in tools]} if tools else None,
    }, source="plugin", identity={"gateway_id": "gateway", "runtime_id": "runtime",
                                  "session_id": session, "run_id": run})


def setup(*, overhead=100):
    hub = SignalHub()
    now = [100.0]
    hub.sessions = SessionStateManager(hub.publish, clock=lambda: now[0])
    if overhead is not None:
        hub.sessions.handoff_history[("gateway", "runtime")].append(overhead)
    return hub, hub.sessions, now


def progress(manager, message, tool, now, remaining=1500, validity=1):
    owner = {**message["identity"], "tool_call_id": tool}
    manager.progress(owner, {"remaining_duration_ms": remaining}, now, now + validity)


def latest(manager):
    return manager.snapshots()[-1]


def test_parallel_uses_all_lattice_predictions_and_live_revisions():
    hub, manager, now = setup()
    message = observation(predicted={"a": 1200, "b": 1800})
    manager.observe(message)
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1900)
    assert latest(manager)["payload"]["prediction_model"] == "lattice_then_tool"
    assert latest(manager)["payload"]["prediction_source"] == "lattice"
    progress(manager, message, "a", now[0])
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1900)
    progress(manager, message, "b", now[0], remaining=1800)
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1900)
    manager.process_exited({**message["identity"], "tool_call_id": "a"})
    assert latest(manager)["payload"]["phase"] == "TOOLS"
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1900)
    manager.observe(observation(revision=2, settled=("a",),
                                predicted={"a": 1200, "b": 1800}))
    assert latest(manager)["payload"]["phase"] == "TOOLS"
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1900)
    manager.observe(observation(revision=3, settled=("a", "b"),
                                predicted={"a": 1200, "b": 1800}))
    assert latest(manager)["payload"]["phase"] == "HANDOFF"
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(100)
    for m in hub.recent:
        validate(m, "signal.schema.json")


def test_batch_reports_mixed_when_lattice_and_tool_fallback_are_both_used():
    _, manager, _ = setup()
    message = observation(predicted={"a": 1200, "b": 1800})
    message["payload"]["batch"]["tools"][1]["prediction_source"] = "tool"
    manager.observe(message)
    assert latest(manager)["payload"]["prediction_source"] == "mixed"


def test_sequential_includes_pending_lattice_predictions_and_expires_stale_evidence():
    _, manager, now = setup()
    message = observation(started=("a",), predicted={"a": 1000, "b": 500},
                          mode="sequential", mode_source="observed_sequence")
    manager.observe(message)
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1600)
    progress(manager, message, "a", now[0])
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(2100)
    manager.observe(observation(revision=2, started=("a",), settled=("a",),
                                predicted={"a": 1000, "b": 500}, mode="sequential",
                                mode_source="observed_sequence"))
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(600)
    manager.observe(observation(revision=3, started=("a", "b"), settled=("a",),
                                predicted={"a": 1000, "b": 500}, mode="sequential",
                                mode_source="observed_sequence"))
    progress(manager, message, "b", now[0], remaining=500)
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(600)
    now[0] += 1.1
    assert latest(manager)["payload"]["remaining_ms"] is None
    manager.observe(observation(revision=4, foreground=False, settled=("a", "b"),
                                predicted={"a": 1000, "b": 500}, mode="sequential",
                                mode_source="observed_sequence"))
    assert latest(manager)["payload"]["phase"] == "HANDOFF"  # background handle returned
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(100)


def test_expired_live_progress_falls_back_to_selected_historical_prediction():
    _, manager, now = setup()
    message = observation(tools=("a",), started=("a",), predicted=3000)
    manager.observe(message)
    progress(manager, message, "a", now[0], remaining=500, validity=.5)
    assert latest(manager)["payload"]["forecast_basis"] == "live_progress"
    now[0] += .6
    payload = latest(manager)["payload"]
    assert payload["forecast_basis"] == "pre_execution"
    assert payload["remaining_ms"] == pytest.approx(2500)


def test_consumed_or_withdrawn_batch_cannot_reappear_or_calibrate():
    _, manager, now = setup()
    first = observation(tools=("a",), settled=("a",))
    manager.observe(first)
    now[0] += .1
    manager.model_started(first["identity"], "model-next", now[0])
    assert latest(manager)["payload"]["phase"] == "LLM"
    samples = list(manager.handoff_history[("gateway", "runtime")])
    manager.model_completed(first["identity"], "model-next", now[0])
    assert latest(manager)["payload"]["phase"] == "CONTROL"
    assert latest(manager)["payload"]["reason"] == "batch_target_resolved"
    assert list(manager.handoff_history[("gateway", "runtime")]) == samples

    second = observation(revision=2, tools=("b",))
    manager.observe(second)
    manager.observe(observation(revision=3, tools=("b",), status="yielded"))
    now[0] += .2
    manager.model_started(second["identity"], "late-model", now[0])
    assert list(manager.handoff_history[("gateway", "runtime")]) == samples
    assert latest(manager)["payload"]["phase"] == "WAIT_EVENT"


def test_foreground_retraction_and_stale_heartbeat():
    _, manager, now = setup()
    message = observation(tools=("a",))
    manager.observe(message)
    progress(manager, message, "a", now[0])
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(1600)
    manager.observe(observation(revision=2, tools=("a",), foreground=False,
                                predicted=2000))
    assert latest(manager)["payload"]["remaining_ms"] == pytest.approx(2100)
    assert latest(manager)["payload"]["forecast_basis"] == "pre_execution"
    now[0] += 16
    stale = observation(revision=3, status="ended")
    stale["observed_at"] = 100
    assert not manager.observe(stale)
    assert latest(manager)["payload"]["phase"] == "UNKNOWN"


def test_cold_handoff_stays_unknown_then_first_model_start_calibrates():
    _, manager, now = setup(overhead=None)
    first = observation(tools=("a",), started=("a",), predicted=800)
    manager.observe(first)
    assert latest(manager)["payload"]["remaining_ms"] is None
    assert latest(manager)["payload"]["unavailable_reason"] == "handoff_overhead_unavailable"
    manager.observe(observation(revision=2, tools=("a",), settled=("a",), predicted=800))
    now[0] += .25
    manager.model_started(first["identity"], "model-next", now[0])
    payload = latest(manager)["payload"]
    assert payload["phase"] == "LLM"
    assert payload["handoff_from_batch_id"] == "run:batch"
    assert manager.handoff_history[("gateway", "runtime")][0] == pytest.approx(250)


def test_session_only_delivery_keeps_internal_tool_observations(monkeypatch):
    monkeypatch.setenv("CLAWTUNE_SIGNAL_TOOL_EVENTS", "false")
    assert SidecarConfig.from_env().signal_tool_events is False
    hub = SignalHub("http://receiver", tool_events=False)
    hub.publish("model.started", {})
    hub.sessions.observe(observation())
    assert hub.queue.empty()
    assert hub.task is None
    assert len(hub.session_pending) == 1
    assert [m["type"] for m in hub.recent] == [
        "model.started", "session.state", "session.handoff_forecast"]


def test_raw_progress_refreshes_session_after_one_shot_tool_notification():
    hub = SignalHub()
    hub.sessions.handoff_history[("gateway", "runtime")].append(100)
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
    assert len(forecasts) >= 3
    assert forecasts[-1]["payload"]["remaining_ms"] > forecasts[-2]["payload"]["remaining_ms"]
    # Reused call IDs must not attach an old execution to a later assistant batch.
    next_batch = observation(revision=2, tools=("a",))
    next_batch["payload"]["batch"]["batch_id"] = "run:next-batch"
    hub.sessions.observe(next_batch)
    assert hub.observe(envelope("execution.progress", {
        "elapsed_duration_ms": 600, "remaining_duration_ms": 1800,
        "remaining_source": "pytest.completed_percent", "remaining_kind": "estimated",
    }, source="launcher", identity=owner, observed_at=now + .002, expires_at=now + 1))
    assert latest(hub.sessions)["payload"]["batch_id"] == "run:next-batch"
    assert latest(hub.sessions)["payload"]["forecast_basis"] == "pre_execution"


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
    invalid_wait = observation(revision=2, status="yielded")
    invalid_wait["payload"]["wait"] = {
        "reason": "subagent_completion", "mode": "any", "session_keys": ["missing-child"]}
    with pytest.raises(ValueError, match="invalid_session_wait"):
        manager.observe(invalid_wait)
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


def test_read_only_preview_uses_predictor_without_execution_side_effects(tmp_path, monkeypatch):
    config = SidecarConfig(trace_dir=tmp_path / "traces", auth_token="secret",
                           tool_resource_ebpf_enabled=False, pmu_enabled=False)
    state = build_state(config)
    root = Path(__file__).resolve().parents[3]
    request = json.loads((root / "contracts/examples/tool-before-request.json").read_text())
    prediction = ToolPrediction.model_validate(json.loads(
        (root / "contracts/examples/tool-prediction.json").read_text()))
    predicted = []
    monkeypatch.setattr(state.predictor, "predict_session_load",
                        lambda value: predicted.append(value) or prediction.lattice.load)
    monkeypatch.setattr(state.predictor, "record_tool_started",
                        lambda *_: pytest.fail("preview recorded a tool start"))
    monkeypatch.setattr(state.tool_monitor, "begin",
                        lambda *_: pytest.fail("preview started monitoring"))
    with TestClient(create_app(state)) as client:
        response = client.post("/v1/predictions/tool", json=request,
                               headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.json()["targets"]["duration_ms"] == (
        prediction.lattice.load.targets["duration_ms"].model_dump(mode="json"))
    assert len(predicted) == 1
    assert predicted[0].tool_call_id == request["tool_call_id"]


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
