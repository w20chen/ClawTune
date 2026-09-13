from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig


BODY = {"reason": "task_timeout", "agent_stopped": True, "sandbox_cleaned": True}
PATH = "/v1/gateways/swe-rebench/runtimes/task-a"


def register(client, execution_id, runtime="task-a", gateway="swe-rebench"):
    registered = client.post("/v2/executions", json={
        "execution_id": execution_id, "tool_call_id": "call-" + execution_id,
        "run_id": "run", "session_key_hash": None, "runtime_id": runtime,
        "gateway_id": gateway, "command_digest": "sha256:" + "a" * 64,
        "command": "sleep 30", "backend": "managed-wrapper",
    })
    assert registered.status_code == 200
    claimed = client.post("/v2/executions/claim", json={
        "execution_id": execution_id, "token": registered.json()["one_time_token"],
        "launcher_pid": 1234,
    })
    assert claimed.status_code == 200
    return claimed.json()["update_token"]


def test_abort_closes_only_unfinished_owner_and_preserves_completed_data(monkeypatch, tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    active = {"unfinished", "foreign"}
    finished, flushes = [], []
    monkeypatch.setattr(state.predictor, "execution_active", lambda eid: eid in active)
    monkeypatch.setattr(state.predictor, "flush_kb_updates", lambda *a, **k: flushes.append(True))
    def finish(**kwargs):
        finished.append(kwargs)
        active.remove(kwargs["execution_id"])
    monkeypatch.setattr(state.predictor, "finish_execution", finish)
    with TestClient(create_app(state)) as client:
        token = register(client, "unfinished")
        done_token = register(client, "done")
        register(client, "foreign", gateway="another-gateway")
        client.post("/v2/executions/done/exited", json={"update_token": done_token, "exit_code": 0, "signal": None})
        assert client.post(PATH + "/drain?timeout_seconds=0").json()["drained"] is False
        result = client.post(PATH + "/abort", json=BODY)
        assert result.status_code == 200, result.text
        assert result.json()["aborted_execution_ids"] == ["unfinished"]
        assert state.executions.get("unfinished").aborted_reason == "task_timeout"
        assert state.executions.get("unfinished").exit_code is None
        assert state.executions.get("done").exit_code == 0
        assert not state.executions.get("foreign").exited
        assert active == {"foreign"}
        assert finished == [{"execution_id": "unfinished", "exit_code": None,
                             "signal": None, "incomplete_reason": "task_timeout"}]
        assert client.post(PATH + "/abort", json=BODY).json() == result.json()
        assert len(finished) == 1
        assert client.post(PATH + "/drain?timeout_seconds=0").json()["drained"] is True
        assert flushes == [True]
        assert client.post("/v2/executions/unfinished/exited", json={
            "update_token": token, "exit_code": 0, "signal": None,
        }).status_code == 409
        contracts = Path(__file__).resolve().parents[3] / "contracts"
        schema = json.loads((contracts / "runtime-abort-response.schema.json").read_text())
        schema["properties"]["pmu_profiles"]["additionalProperties"] = json.loads(
            (contracts / "pmu-profile.schema.json").read_text())
        Draft202012Validator(schema).validate(result.json())


@pytest.mark.parametrize("field", ["agent_stopped", "sandbox_cleaned"])
def test_abort_requires_both_shutdown_assertions(tmp_path, field):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    with TestClient(create_app(state)) as client:
        register(client, "unfinished")
        assert client.post(PATH + "/abort", json={**BODY, field: False}).status_code == 422
        assert not state.executions.get("unfinished").exited


def test_abort_finishes_owned_collectors_even_after_registry_retention(monkeypatch, tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    active = {"expired", "exited", "foreign"}
    finished = []
    monkeypatch.setattr(state.predictor, "execution_active", lambda eid: eid in active)
    monkeypatch.setattr(state.predictor, "active_execution_ids",
                        lambda runtime, gateway: tuple(active - {"foreign"}))
    def finish(**kwargs):
        active.remove(kwargs["execution_id"])
        finished.append(kwargs)
    monkeypatch.setattr(state.predictor, "finish_execution", finish)
    with TestClient(create_app(state)) as client:
        register(client, "exited")
        record = state.executions.get("exited")
        record.exited, record.exit_code = True, 125
        response = client.post(PATH + "/abort", json=BODY)
        assert response.status_code == 200, response.text
        assert response.json()["aborted_execution_ids"] == ["expired"]
        assert record.exit_code == 125 and record.aborted_reason is None
        assert active == {"foreign"}
        assert finished == [
            {"execution_id": "exited", "exit_code": 125, "signal": None},
            {"execution_id": "expired", "exit_code": None, "signal": None,
             "incomplete_reason": "task_timeout"},
        ]
        assert client.post(PATH + "/drain?timeout_seconds=0&flush_kb=false").json()["drained"] is True


def test_orphan_finish_error_cannot_disappear_on_abort_retry(monkeypatch, tmp_path):
    from types import SimpleNamespace
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    active = {"expired"}
    monkeypatch.setattr(state.predictor, "execution_active", lambda eid: eid in active)
    monkeypatch.setattr(state.predictor, "active_execution_ids", lambda *args: tuple(active))
    monkeypatch.setattr(state.predictor, "finish_execution", lambda **kwargs: active.clear())
    monkeypatch.setattr(state.predictor, "execution_telemetry",
                        lambda eid: SimpleNamespace(kb_update_error="disk write failed"))
    with TestClient(create_app(state)) as client:
        assert client.post(PATH + "/abort", json=BODY).status_code == 503
        assert not active
        assert client.post(PATH + "/abort", json=BODY).status_code == 503
        assert client.post(PATH + "/drain?timeout_seconds=0").json()["drained"] is False


@pytest.mark.parametrize("busy", ["cgroup", "request", "legacy_owner"])
def test_abort_rejects_live_or_ambiguous_runtime(tmp_path, busy):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    with TestClient(create_app(state)) as client:
        register(client, "unfinished", gateway=None if busy == "legacy_owner" else "swe-rebench")
        record = state.executions.get("unfinished")
        if busy == "cgroup":
            group = tmp_path / "group"
            group.mkdir()
            (group / "cgroup.events").write_text("populated 1\n")
            record.owned_cgroup_path = str(group)
        elif busy == "request":
            state._runtime_activity[("swe-rebench", "task-a")] = 1
        assert client.post(PATH + "/abort", json=BODY).status_code == 409
        assert not record.exited
        state._runtime_activity.clear()


def test_drain_waits_for_abort_worker_and_late_start_is_rejected(monkeypatch, tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    entered, release = Event(), Event()
    active = {"unfinished"}
    monkeypatch.setattr(state.predictor, "execution_active", lambda eid: eid in active)
    def finish(**kwargs):
        active.clear()  # Predictor relinquishes its run before finishing disk writes.
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(state.predictor, "finish_execution", finish)
    with TestClient(create_app(state)) as client, ThreadPoolExecutor() as pool:
        token = register(client, "unfinished")
        future = pool.submit(client.post, PATH + "/abort", json=BODY)
        try:
            assert entered.wait(5)
            assert client.post(PATH + "/drain?timeout_seconds=0").json()["drained"] is False
            late = client.post("/v2/executions/unfinished/started", json={
                "update_token": token, "launcher_pid": 1234, "child_pid": 1235,
            })
            assert late.status_code == 409, late.text
        finally:
            release.set()
        assert future.result().status_code == 200
        assert client.post(PATH + "/drain?timeout_seconds=0&flush_kb=false").json()["drained"] is True


def test_failed_finalization_keeps_drain_closed_until_successful_retry(monkeypatch, tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    active = {"unfinished"}
    attempts = []
    monkeypatch.setattr(state.predictor, "execution_active", lambda eid: eid in active)
    def finish(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError("collector cleanup failed")
        active.clear()
    monkeypatch.setattr(state.predictor, "finish_execution", finish)
    with TestClient(create_app(state), raise_server_exceptions=False) as client:
        register(client, "unfinished")
        assert client.post(PATH + "/abort", json=BODY).status_code == 500
        assert client.post(PATH + "/drain?timeout_seconds=0").json()["drained"] is False
        assert client.post(PATH + "/abort", json=BODY).status_code == 200
        assert client.post(PATH + "/drain?timeout_seconds=0&flush_kb=false").json()["drained"] is True
