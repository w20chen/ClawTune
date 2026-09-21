"""Native tools share SWE monitoring without inventing exec/PMU events."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from clawtune_sidecar.api import app as api
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.contracts.models import NativeRuntimeScopeRequest, ResourceScope


IDENTITY = {"pid": 42, "pid_namespace_inode": 123, "process_starttime_ticks": 456}
ENDPOINT = "/v1/gateways/bfcl/runtimes/task/native-scope"


def test_native_request_matches_public_schema():
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts/native-runtime-scope.schema.json").read_text())
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(IDENTITY))
    assert NativeRuntimeScopeRequest.model_validate(IDENTITY).model_dump() == IDENTITY
    for data in ({**IDENTITY, "pid": 0}, {"pid": 42}, {**IDENTITY, "cgroup_path": "/host"}):
        assert list(validator.iter_errors(data))
        with pytest.raises(ValueError):
            NativeRuntimeScopeRequest.model_validate(data)


@pytest.fixture
def native(monkeypatch, tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    scope = ResourceScope(kind="cgroup-v2", pid=42, root_pid=42,
        root_starttime_ticks=456, pid_namespace_inode=123,
        cgroup_path=str(tmp_path / "env" / "native"),
        attribution_source="exclusive-execution-cgroup")
    monkeypatch.setattr(api, "_resolve_host_pid", lambda *a, **kw: 42)
    monkeypatch.setattr(api, "_prepare_host_execution_cgroup", lambda *a, **kw: scope)
    monkeypatch.setattr(api, "_cleanup_owned_cgroup", lambda path: True)
    with TestClient(api.create_app(state)) as client:
        yield client, state, scope


def test_native_scope_is_idempotent_isolated_and_not_an_execution(native):
    client, state, scope = native
    first = client.post(ENDPOINT, json=IDENTITY)
    assert first.status_code == 200
    assert first.json()["attribution_source"] == "exclusive-task-cgroup"
    assert first.json()["execution_id"] is None
    assert client.post(ENDPOINT, json=IDENTITY).json() == first.json()
    assert set(state._sandbox_scopes_by_owner) == {("bfcl", "task")}
    assert not list(state.executions.active())
    conflict = client.post(ENDPOINT, json={**IDENTITY, "process_starttime_ticks": 999})
    assert conflict.status_code == 409
    assert client.delete(ENDPOINT).json() == {"stored": True}
    assert not state._sandbox_scopes_by_owner
    assert not state._native_runtime_scopes


def test_native_identity_and_cgroup_fail_closed(native, monkeypatch):
    client, state, scope = native
    monkeypatch.setattr(api, "_resolve_host_pid", lambda *a, **kw: None)
    assert client.post(ENDPOINT, json=IDENTITY).status_code == 422
    monkeypatch.setattr(api, "_resolve_host_pid", lambda *a, **kw: 42)
    monkeypatch.setattr(api, "_prepare_host_execution_cgroup", lambda *a, **kw: None)
    assert client.post(ENDPOINT, json=IDENTITY).status_code == 503
    assert not state._sandbox_scopes_by_owner


def test_native_cleanup_keeps_scope_until_worker_stops(native, monkeypatch):
    client, state, scope = native
    assert client.post(ENDPOINT, json=IDENTITY).status_code == 200
    monkeypatch.setattr(api, "_cleanup_owned_cgroup", lambda path: False)
    assert client.delete(ENDPOINT).status_code == 409
    assert ("bfcl", "task") in state._native_runtime_scopes


def test_native_tool_uses_worker_scope_not_openclaw_host(native, monkeypatch):
    from test_tool_runtime_monitor import _request
    client, state, scope = native
    assert client.post(ENDPOINT, json=IDENTITY).status_code == 200
    observed = []
    original = state.tool_monitor.begin
    def begin(request, *args, **kwargs):
        observed.append(request.resource_scope)
        return original(request, *args, **kwargs)
    monkeypatch.setattr(state.tool_monitor, "begin", begin)
    for tool in ("add", "web_search"):
        event = _request(ResourceScope(pid=999, source="openclaw-runtime")).model_copy(update={
            "gateway_id": "bfcl", "runtime_id": "task", "tool_name": tool,
            "event_id": tool, "tool_call_id": tool})
        response = client.post("/v1/decisions/tool", json=event.model_dump())
        assert response.status_code == 200, response.text
    assert len(observed) == 2
    assert all(s.root_pid == 42 and s.attribution_source == "exclusive-task-cgroup" for s in observed)
