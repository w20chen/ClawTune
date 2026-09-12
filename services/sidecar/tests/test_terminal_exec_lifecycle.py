from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.contracts.models import (
    ExecutionClaimRequest,
    ExecutionRegistrationRequest,
    ParamFeatures,
    ResourceScope,
    ToolBeforeRequest,
)
from clawtune_sidecar.executions import ExecutionRegistry
from clawtune_sidecar.monitoring.docker_exec import DockerExecObserver


def _registration(
    execution_id: str = "terminal-1",
    *,
    runtime_id: str = "runtime-1",
    gateway_id: str = "swe-rebench",
    tool_call_id: str = "call-terminal",
) -> ExecutionRegistrationRequest:
    return ExecutionRegistrationRequest(
        execution_id=execution_id,
        gateway_id=gateway_id,
        runtime_id=runtime_id,
        tool_call_id=tool_call_id,
        run_id=None,
        session_key_hash=None,
        command_digest="sha256:" + "a" * 64,
        command="echo hi",
        backend="managed-wrapper",
    )


def _claimed_registry(execution_id: str = "terminal-1") -> ExecutionRegistry:
    registry = ExecutionRegistry()
    response = registry.register(_registration(execution_id))
    registry.claim(
        ExecutionClaimRequest(
            execution_id=execution_id,
            token=response.one_time_token,
            launcher_pid=1,
        )
    )
    return registry


def _event(
    tool_call_id: str = "call-terminal",
    *,
    runtime_id: str = "runtime-1",
    gateway_id: str = "swe-rebench",
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call_id=tool_call_id, runtime_id=runtime_id, gateway_id=gateway_id
    )


def test_unique_tool_call_lookup_requires_a_claimed_match() -> None:
    registry = ExecutionRegistry()
    registry.register(_registration())

    assert registry.unique_tool_call_execution_id(_event()) is None

    registry = _claimed_registry()
    assert registry.unique_tool_call_execution_id(_event()) == "terminal-1"


def test_unique_tool_call_lookup_rejects_foreign_owners() -> None:
    registry = _claimed_registry()

    assert registry.unique_tool_call_execution_id(_event(runtime_id="other")) is None
    assert (
        registry.unique_tool_call_execution_id(_event(gateway_id="other-gateway"))
        is None
    )
    assert registry.unique_tool_call_execution_id(_event("other-call")) is None


def test_unique_tool_call_lookup_rejects_ambiguous_matches() -> None:
    registry = _claimed_registry("terminal-1")
    second = registry.register(_registration("terminal-2"))
    registry.claim(
        ExecutionClaimRequest(
            execution_id="terminal-2", token=second.one_time_token, launcher_pid=1
        )
    )

    assert registry.unique_tool_call_execution_id(_event()) is None


def test_managed_execution_rejects_unverifiable_root_before_arming(monkeypatch, tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces", tool_resource_ebpf_required=False))
    monkeypatch.setattr("clawtune_sidecar.api.app._resolve_host_pid", lambda *a, **kw: None)
    armed = []
    monkeypatch.setattr(state.pmu_collector, "begin", lambda *args: armed.append(args))
    client = TestClient(create_app(state))
    registered = client.post("/v2/executions", json=_registration().model_dump(mode="json")).json()
    claimed = client.post("/v2/executions/claim", json={
        "execution_id": "terminal-1", "token": registered["one_time_token"], "launcher_pid": 1,
    }).json()
    response = client.post("/v2/executions/terminal-1/started", json={
        "update_token": claimed["update_token"], "launcher_pid": 1, "child_pid": 42,
        "process_starttime_ticks": 100, "pid_namespace_inode": 123,
        "container_id": "client",
    })
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_execution_root_identity"
    assert armed == []
    assert state.executions.scope("terminal-1") is None


def _write_cgroup_fixture(path: Path, usage_usec: int = 100_000) -> None:
    path.mkdir()
    (path / "cpu.stat").write_text(f"usage_usec {usage_usec}\n", encoding="utf-8")
    (path / "memory.current").write_text("4096\n", encoding="utf-8")
    (path / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (path / "cgroup.procs").write_text("", encoding="utf-8")


def test_bridge_completion_adopts_the_registered_execution(tmp_path: Path) -> None:
    """The harness registers an execution the plugin completion cannot name."""

    cgroup = tmp_path / "sandbox-cgroup"
    _write_cgroup_fixture(cgroup)
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            sandbox_cgroup_path=str(cgroup),
            sandbox_container_id="sandbox-1",
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "terminal-1",
            "gateway_id": "swe-rebench",
            "runtime_id": "runtime-1",
            "tool_call_id": "call-terminal",
            "run_id": None,
            "session_key_hash": None,
            "command_digest": "sha256:" + "a" * 64,
            "command": "echo hi",
            "backend": "managed-wrapper",
        },
    ).json()
    claim = client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "terminal-1",
            "token": registration["one_time_token"],
            "launcher_pid": 1,
        },
    ).json()
    assert claim["update_token"]
    # /started normally resolves this scope from the gated root PID.
    state.executions.update_scope(
        "terminal-1",
        ResourceScope(
            kind="pid",
            execution_id="terminal-1",
            pid=os.getpid(),
            root_pid=os.getpid(),
            include_children=True,
            source="clawtune-sidecar-host-derived",
            attribution_source="trusted-execution-root-pid",
        ),
    )
    request = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-terminal-start",
        "occurred_at": "2026-09-12T03:23:00Z",
        "plugin_version": "0.1.0",
        "gateway_id": "swe-rebench",
        "run_id": "run-terminal",
        "session_id": "session-terminal",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-terminal",
        "tool_name": "terminal_exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": "terminal_exec",
        "derived_paths": [],
        "params_digest": "sha256:" + "b" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": {"command": "echo hi"},
        "resource_scope": None,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    taken: list[str] = []
    state.pmu_collector.take = lambda execution_id: taken.append(execution_id) or None
    completion = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-terminal-end",
        "occurred_at": "2026-09-12T03:23:01Z",
        "plugin_version": "0.1.0",
        "gateway_id": "swe-rebench",
        "run_id": "run-terminal",
        "session_id": "session-terminal",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-terminal",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "terminal_exec",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 4,
        "raw_result": {"exit_code": 0},
        "resource_scope": None,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {
        "stored": True
    }
    # PMU evidence is only consumed for a named execution; adoption is what
    # makes the finalized profile visible to the predictor and the trace.
    assert taken and set(taken) == {"terminal-1"}
    record = state.executions.get("terminal-1")
    assert record is not None and record.exited is True


def _tool_request(tool_call_id: str, cgroup_path: str, container_id: str) -> ToolBeforeRequest:
    return ToolBeforeRequest(
        schema_version="clawtune.v1",
        event_id=f"evt-{tool_call_id}",
        occurred_at="2026-09-12T03:23:00Z",
        plugin_version="0.1.0",
        gateway_id="swe-rebench",
        run_id="run-diff",
        session_id="session-diff",
        runtime_id="runtime-1",
        session_key=None,
        agent_id=None,
        tool_call_id=tool_call_id,
        tool_name="terminal_exec",
        tool_kind="shell",
        tool_input_kind="json",
        operation_hint=None,
        derived_paths=[],
        params_digest="sha256:" + "c" * 64,
        param_features=ParamFeatures(
            serialized_size_bytes=10,
            string_length=5,
            list_item_count=0,
            path_count=0,
            has_command_like_field=True,
        ),
        raw_params={"command": "echo hi"},
        resource_scope=ResourceScope(
            kind="cgroup-v2",
            cgroup_path=cgroup_path,
            container_id=container_id,
            include_children=True,
            source="openclaw-sandbox",
            attribution_source="shared-sandbox-container",
        ),
    )


def test_cgroup_diff_tracks_each_tools_own_container(tmp_path: Path) -> None:
    cgroup_a = tmp_path / "task-a"
    cgroup_b = tmp_path / "task-b"
    cgroup_a.mkdir()
    cgroup_b.mkdir()
    (cgroup_a / "cgroup.procs").write_text("100\n", encoding="utf-8")
    (cgroup_b / "cgroup.procs").write_text("200\n", encoding="utf-8")
    observer = DockerExecObserver(enabled=True, cgroup_path=str(cgroup_a), autostart=False)
    observer.begin_tool(_tool_request("call-a", str(cgroup_a), "container-a"))
    observer.begin_tool(_tool_request("call-b", str(cgroup_b), "container-b"))
    (cgroup_a / "cgroup.procs").write_text("100\n111\n", encoding="utf-8")
    (cgroup_b / "cgroup.procs").write_text("200\n222\n", encoding="utf-8")

    observer._poll_cgroup_once()

    scope_a = observer.infer_scope(_completion("call-a"))
    scope_b = observer.infer_scope(_completion("call-b"))
    assert scope_a is not None and scope_a.root_pid == 111
    assert scope_a.container_id == "container-a"
    assert scope_b is not None and scope_b.root_pid == 222
    assert scope_b.container_id == "container-b"
    assert observer.diagnostics()["cgroup_diff_captures"] == 2


def _completion(tool_call_id: str) -> SimpleNamespace:
    """Minimal completion view: infer_scope reads only identity fields."""

    return SimpleNamespace(
        execution_id=None,
        resource_scope=None,
        tool_call_id=tool_call_id,
        tool_name="terminal_exec",
        runtime_id="runtime-1",
        gateway_id="swe-rebench",
    )


def test_empty_container_prefix_matches_named_events(monkeypatch, tmp_path: Path) -> None:
    observer = DockerExecObserver(enabled=True, container_prefix="", autostart=False)
    monkeypatch.setattr(
        observer,
        "_inspect_exec",
        lambda exec_id: {
            "Pid": os.getpid(),
            "ContainerID": "container-full",
            "ProcessConfig": {"entrypoint": "sh", "arguments": ["-c", "echo hi"]},
        },
    )

    observer._handle_event_line(
        json.dumps(
            {
                "status": "start",
                "id": "container-full",
                "Type": "container",
                "Action": "exec_start",
                "Actor": {
                    "ID": "container-full",
                    "Attributes": {"execID": "exec-1", "name": "ct-abc-client"},
                },
            }
        )
    )

    assert observer.diagnostics()["event_lines_seen"] == 1
    assert [record.exec_id for record in observer._records] == ["exec-1"]
