from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_scheduler.api.app import create_app
from agent_scheduler.api.dependencies import build_state
from agent_scheduler.config import SchedulerConfig
from agent_scheduler.llm_proxy import _forward_headers, _upstream_url
from agent_scheduler.monitoring.docker_exec import DockerExecObserver, _docker_events_command
from agent_scheduler.monitoring.tool_runtime import _relative_timeline


def _read_trace_records(trace_dir: Path) -> list[dict]:
    """Find the first JSONL file in trace_dir and return parsed records."""
    files = list(trace_dir.glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def _client(tmp_path: Path) -> TestClient:
    state = build_state(SchedulerConfig(trace_dir=tmp_path / "traces"))
    return TestClient(create_app(state))


def _trace_client(tmp_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(SchedulerConfig(trace_dir=trace_dir))
    return TestClient(create_app(state)), trace_dir


def _trace_proxy_client(tmp_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SchedulerConfig(
            trace_dir=trace_dir,
            llm_proxy_upstream_base_url="https://upstream.example/v1",
        )
    )
    return TestClient(create_app(state)), trace_dir


def _trace_client_with_sandbox_cgroup(tmp_path: Path, cgroup_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SchedulerConfig(
            trace_dir=trace_dir,
            sandbox_cgroup_path=str(cgroup_path),
            sandbox_container_id="sandbox-1",
        )
    )
    return TestClient(create_app(state)), trace_dir


def _write_cgroup_fixture(path: Path, usage_usec: int = 100_000) -> None:
    path.mkdir()
    (path / "cpu.stat").write_text(f"usage_usec {usage_usec}\n", encoding="utf-8")
    (path / "memory.current").write_text("4096\n", encoding="utf-8")
    (path / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (path / "cgroup.procs").write_text("", encoding="utf-8")


def _trace_proxy_client_with_debug(tmp_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SchedulerConfig(
            trace_dir=trace_dir,
            llm_proxy_upstream_base_url="https://upstream.example/v1",
            llm_proxy_debug_dump=True,
        )
    )
    return TestClient(create_app(state)), trace_dir


def test_llm_proxy_upstream_url_preserves_v1_when_base_omits_it() -> None:
    assert (
        _upstream_url(
            SchedulerConfig(llm_proxy_upstream_base_url="https://api.deepseek.com"),
            "/v1/chat/completions",
        )
        == "https://api.deepseek.com/v1/chat/completions"
    )
    assert (
        _upstream_url(
            SchedulerConfig(llm_proxy_upstream_base_url="https://api.deepseek.com/v1"),
            "/v1/chat/completions",
        )
        == "https://api.deepseek.com/v1/chat/completions"
    )


def test_llm_proxy_forwards_openclaw_authorization_header_by_default() -> None:
    class Request:
        headers = {
            "authorization": "Bearer sk-openclaw",
            "content-type": "application/json",
        }

    headers_without_upstream_key = _forward_headers(Request(), SchedulerConfig())
    assert headers_without_upstream_key["authorization"] == "Bearer sk-openclaw"
    assert headers_without_upstream_key["content-type"] == "application/json"


def test_llm_proxy_upstream_key_overrides_openclaw_authorization_header() -> None:
    class Request:
        headers = {
            "authorization": "Bearer sk-openclaw",
            "content-type": "application/json",
        }

    headers = _forward_headers(
        Request(),
        SchedulerConfig(llm_proxy_upstream_api_key="real-key"),
    )

    assert headers["authorization"] == "Bearer real-key"
    assert headers["content-type"] == "application/json"


def test_decision_and_completion_round_trip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-1",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": None,
        "session_id": None,
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-1",
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": "pytest",
        "derived_paths": [],
        "params_digest": "sha256:" + "a" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": None,
        "resource_scope": None,
    }
    decision_response = client.post("/v1/decisions/tool", json=request)
    assert decision_response.status_code == 200
    decision = decision_response.json()
    assert decision["action"] == "allow"

    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-2",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": None,
        "session_id": None,
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-1",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "exec",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": None,
    }
    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}
    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": False}
    recent = client.get("/v1/tools/recent").json()
    assert len(recent["samples"]) == 1
    sample = recent["samples"][0]
    assert sample["tool_call_id"] == "call-1"
    assert sample["tool_name"] == "exec"
    assert sample["duration_ms"] == 100
    assert sample["resource_class"] == "unknown"
    assert sample["attribution_status"] == "unattributed"

    request_without_tool_call_id = request | {
        "event_id": "evt-3",
        "tool_call_id": None,
        "params_digest": "sha256:" + "b" * 64,
        "resource_scope": {
            "pid": os.getpid(),
            "process_start_time": None,
            "container_id": None,
            "include_children": True,
            "source": "test",
        },
    }
    second_decision = client.post("/v1/decisions/tool", json=request_without_tool_call_id).json()
    completion_without_tool_call_id = completion | {
        "event_id": "evt-4",
        "tool_call_id": None,
        "decision_id": second_decision["decision_id"],
        "lease_id": second_decision["lease_id"],
    }
    assert client.post("/v1/events/tool-completed", json=completion_without_tool_call_id).json() == {
        "stored": True
    }
    recent = client.get("/v1/tools/recent").json()
    assert len(recent["samples"]) == 2
    assert recent["samples"][0]["target_pid"] == os.getpid()


def test_metrics_endpoint(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "scheduler_tool_requests_total" in response.text
    assert "scheduler_tool_runtime_samples_total" in response.text
    assert "scheduler_tool_runtime_unattributed_samples_total" in response.text
    assert "scheduler_tool_cpu_seconds_total" in response.text
    assert "scheduler_tool_memory_rss_bytes" in response.text
    assert "scheduler_tool_memory_rss_peak_bytes" in response.text
    assert "scheduler_tool_process_count" in response.text
    assert "scheduler_tool_cpu_utilization_avg_cores" in response.text
    assert "scheduler_tool_net_rx_bytes_total" in response.text
    assert "scheduler_tool_net_tx_bytes_total" in response.text
    assert "scheduler_tool_io_write_bytes_per_second" in response.text
    assert "scheduler_tool_net_tx_bytes_per_second" in response.text


def test_internal_tool_uses_shared_sandbox_cgroup_fallback(tmp_path: Path) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 100000\n", encoding="utf-8")
    (cgroup / "memory.current").write_text("4096\n", encoding="utf-8")
    (cgroup / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    client, trace_dir = _trace_client_with_sandbox_cgroup(tmp_path, cgroup)
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-read-start",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-sandbox",
        "session_id": "session-sandbox",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read",
        "tool_name": "read",
        "tool_kind": "file",
        "tool_input_kind": "json",
        "operation_hint": None,
        "derived_paths": [],
        "params_digest": "sha256:" + "a" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 1,
            "has_command_like_field": False,
        },
        "raw_params": {"path": "README.md"},
        "resource_scope": None,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    (cgroup / "cpu.stat").write_text("usage_usec 200000\n", encoding="utf-8")
    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-read-end",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-sandbox",
        "session_id": "session-sandbox",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "read",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 4,
        "raw_result": "data",
        "resource_scope": None,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    tool_end = [
        record
        for record in _read_trace_records(trace_dir)
        if record.get("record_type") == "span_end" and record.get("kind") == "tool"
    ][0]
    assert tool_end["execution"]["cgroup_path"] == str(cgroup)
    assert tool_end["resources"]["attribution_status"] == "partially_attributed"
    assert tool_end["resources"]["scope"] == "cgroup"
    assert tool_end["resources"]["coverage_reason"] == "shared_sandbox_container"
    assert tool_end["resources"]["monitor_duration_ns"] is not None
    assert tool_end["resources"]["cgroup_cpu_time_s"] is not None


def test_internal_tool_uses_docker_exec_inferred_scope_before_fallback(tmp_path: Path) -> None:
    fallback_cgroup = tmp_path / "fallback-cgroup"
    inferred_cgroup = tmp_path / "inferred-cgroup"
    _write_cgroup_fixture(fallback_cgroup, usage_usec=100_000)
    _write_cgroup_fixture(inferred_cgroup, usage_usec=500_000)
    trace_dir = tmp_path / "traces"
    state = build_state(
        SchedulerConfig(
            trace_dir=trace_dir,
            sandbox_cgroup_path=str(fallback_cgroup),
            sandbox_container_id="sandbox-1",
        )
    )
    state.docker_exec_observer = DockerExecObserver(
        enabled=True,
        container_id="sandbox-1",
        autostart=False,
    )
    client = TestClient(create_app(state))
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-read-start",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-docker-exec",
        "session_id": "session-docker-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read",
        "tool_name": "read",
        "tool_kind": "file",
        "tool_input_kind": "json",
        "operation_hint": None,
        "derived_paths": [],
        "params_digest": "sha256:" + "a" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 1,
            "has_command_like_field": False,
        },
        "raw_params": {"path": "README.md"},
        "resource_scope": None,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    state.docker_exec_observer.record_exec_start(
        exec_id="exec-read-1",
        container_id="sandbox-1",
        pid=os.getpid(),
        cgroup_path=str(inferred_cgroup),
        command="sh -c openclaw-sandbox-fs read README.md",
    )
    (inferred_cgroup / "cpu.stat").write_text("usage_usec 700000\n", encoding="utf-8")
    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-read-end",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-docker-exec",
        "session_id": "session-docker-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "read",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 4,
        "raw_result": "data",
        "resource_scope": None,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    tool_end = [
        record
        for record in _read_trace_records(trace_dir)
        if record.get("record_type") == "span_end" and record.get("kind") == "tool"
    ][0]
    assert tool_end["execution"]["cgroup_path"] == str(inferred_cgroup)
    assert tool_end["execution"]["source"] == "docker-events"
    assert tool_end["resources"]["attribution_source"] == "docker-exec-inferred"
    assert tool_end["resources"]["attribution_status"] == "attributed"
    assert tool_end["resources"]["coverage_reason"] != "shared_sandbox_container"


def test_internal_tool_overrides_shared_runtime_scope_with_docker_exec(tmp_path: Path) -> None:
    runtime_cgroup = tmp_path / "runtime-cgroup"
    inferred_cgroup = tmp_path / "inferred-cgroup"
    _write_cgroup_fixture(runtime_cgroup, usage_usec=100_000)
    _write_cgroup_fixture(inferred_cgroup, usage_usec=500_000)
    trace_dir = tmp_path / "traces"
    state = build_state(
        SchedulerConfig(
            trace_dir=trace_dir,
            sandbox_cgroup_path=str(runtime_cgroup),
            sandbox_container_id="sandbox-1",
        )
    )
    state.docker_exec_observer = DockerExecObserver(
        enabled=True,
        container_id="sandbox-1",
        autostart=False,
    )
    client = TestClient(create_app(state))
    shared_runtime_scope = {
        "kind": "cgroup-v2",
        "execution_id": None,
        "pid": os.getpid(),
        "root_pid": os.getpid(),
        "process_start_time": None,
        "root_starttime_ticks": None,
        "cgroup_path": str(runtime_cgroup),
        "pid_namespace_inode": None,
        "container_id": None,
        "include_children": True,
        "source": "openclaw-runtime",
        "attribution_source": "shared-runtime-process",
    }
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-read-start",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-docker-exec",
        "session_id": "session-docker-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read",
        "tool_name": "read",
        "tool_kind": "file",
        "tool_input_kind": "json",
        "operation_hint": None,
        "derived_paths": [],
        "params_digest": "sha256:" + "a" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 1,
            "has_command_like_field": False,
        },
        "raw_params": {"path": "README.md"},
        "resource_scope": shared_runtime_scope,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    state.docker_exec_observer.record_exec_start(
        exec_id="exec-read-1",
        container_id="sandbox-1",
        pid=os.getpid(),
        cgroup_path=str(inferred_cgroup),
        command="sh -c openclaw-sandbox-fs read README.md",
    )
    (inferred_cgroup / "cpu.stat").write_text("usage_usec 700000\n", encoding="utf-8")
    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-read-end",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-docker-exec",
        "session_id": "session-docker-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "read",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 4,
        "raw_result": "data",
        "resource_scope": shared_runtime_scope,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    tool_end = [
        record
        for record in _read_trace_records(trace_dir)
        if record.get("record_type") == "span_end" and record.get("kind") == "tool"
    ][0]
    assert tool_end["execution"]["cgroup_path"] == str(inferred_cgroup)
    assert tool_end["execution"]["source"] == "docker-events"
    assert tool_end["resources"]["attribution_source"] == "docker-exec-inferred"
    assert tool_end["resources"]["coverage_reason"] != "shared_runtime_process"


def test_docker_exec_event_uses_exec_id_attribute_not_container_id() -> None:
    observer = DockerExecObserver(
        enabled=True,
        container_id="sandbox-1",
        autostart=False,
    )
    inspected: list[str] = []

    def inspect_exec(exec_id: str) -> dict[str, object]:
        inspected.append(exec_id)
        return {
            "Pid": os.getpid(),
            "ContainerID": "sandbox-1",
            "ProcessConfig": {
                "entrypoint": "sh",
                "arguments": ["-c", "openclaw-sandbox-fs read README.md"],
            },
        }

    observer._inspect_exec = inspect_exec  # type: ignore[method-assign]
    observer._handle_event_line(
        json.dumps(
            {
                "id": "sandbox-1",
                "Actor": {
                    "ID": "sandbox-1",
                    "Attributes": {
                        "execID": "exec-real-id",
                        "container": "sandbox-1",
                        "name": "claw-srb-test-1",
                    },
                },
            }
        )
    )

    assert inspected == ["exec-real-id"]
    assert observer._records[0].exec_id == "exec-real-id"


def test_docker_exec_observer_subscribes_to_container_exec_start_events() -> None:
    command = _docker_events_command("docker")

    assert command == [
        "docker",
        "events",
        "--format",
        "{{json .}}",
        "--filter",
        "type=container",
        "--filter",
        "event=exec_start",
    ]
    assert "type=exec" not in command


def test_exec_tool_can_use_shared_sandbox_cgroup_fallback(tmp_path: Path) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 100000\n", encoding="utf-8")
    (cgroup / "memory.current").write_text("4096\n", encoding="utf-8")
    (cgroup / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    client, trace_dir = _trace_client_with_sandbox_cgroup(tmp_path, cgroup)
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-exec-start",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-sandbox-exec",
        "session_id": "session-sandbox-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-exec",
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": "ls",
        "derived_paths": [],
        "params_digest": "sha256:" + "b" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": {"command": "ls"},
        "resource_scope": None,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    (cgroup / "cpu.stat").write_text("usage_usec 200000\n", encoding="utf-8")
    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-exec-end",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-sandbox-exec",
        "session_id": "session-sandbox-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-exec",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": "call-exec",
        "tool_name": "exec",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": None,
        "raw_result": {"details": {"exitCode": 0}},
        "resource_scope": None,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    tool_end = next(
        r
        for r in _read_trace_records(trace_dir)
        if r.get("record_type") == "span_end" and r.get("kind") == "tool"
    )
    assert tool_end["execution"]["mode"] == "launcher"
    assert tool_end["resources"]["attribution_status"] == "partially_attributed"
    assert tool_end["resources"]["scope"] == "cgroup"
    assert tool_end["resources"]["coverage_reason"] == "shared_sandbox_container"


def test_exec_completion_uses_registered_launcher_scope(tmp_path: Path) -> None:
    cgroup = tmp_path / "launcher-cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 100000\n", encoding="utf-8")
    (cgroup / "memory.current").write_text("4096\n", encoding="utf-8")
    (cgroup / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    client, trace_dir = _trace_client(tmp_path)
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-exec-start",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-launcher-exec",
        "session_id": "session-launcher-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-exec",
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": "ls",
        "derived_paths": [],
        "params_digest": "sha256:" + "b" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": {"command": "ls"},
        "resource_scope": None,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-launcher-exec",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "ls",
            "workdir": "/workspace",
            "host": "gateway",
            "placement": None,
            "profiling": {"mode": "off"},
            "backend": "managed-wrapper",
        },
    ).json()
    claim = client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "call-exec",
            "token": registration["one_time_token"],
            "launcher_pid": os.getpid(),
        },
    ).json()
    client.post(
        "/v2/executions/call-exec/started",
        json={
            "update_token": claim["update_token"],
            "launcher_pid": os.getpid(),
            "child_pid": os.getpid(),
            "process_starttime_ticks": 123,
            "cgroup_path": str(cgroup),
            "pid_namespace_inode": 456,
            "container_id": "sandbox-1",
        },
    )
    (cgroup / "cpu.stat").write_text("usage_usec 200000\n", encoding="utf-8")
    assert client.post(
        "/v2/executions/call-exec/exited",
        json={
            "update_token": claim["update_token"],
            "exit_code": 0,
            "signal": None,
        },
    ).json() == {"stored": True}
    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-exec-end",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-launcher-exec",
        "session_id": "session-launcher-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-exec",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": "call-exec",
        "tool_name": "exec",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": None,
        "raw_result": {"details": {"exitCode": 0}},
        "resource_scope": None,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    tool_end = next(
        r
        for r in _read_trace_records(trace_dir)
        if r.get("record_type") == "span_end" and r.get("kind") == "tool"
    )
    assert tool_end["execution"]["source"] == "claw-launch"
    assert tool_end["execution"]["cgroup_path"] == str(cgroup)
    assert tool_end["resources"]["attribution_source"] == "claw-launch"
    assert tool_end["resources"]["attribution_status"] == "attributed"
    assert tool_end["resources"]["scope"] == "cgroup"
    assert tool_end["execution"]["tool_resource"]["execution_id"] == "call-exec"
    assert tool_end["execution"]["tool_resource"]["status"] in {
        "ok",
        "invalid",
        "unavailable",
        "collected_not_eligible",
    }


def test_resource_timeline_uses_interval_rates() -> None:
    timeline = _relative_timeline(
        [
            {
                "ts": 10.0,
                "cpu_time_s": 1.0,
                "rss_bytes": 100,
                "read_bytes": 0,
                "write_bytes": 0,
                "net_rx_bytes": 1_000_000,
                "net_tx_bytes": 2_000_000,
                "ctx_switches": 5,
                "process_count": 1,
                "available": True,
                "source": "psutil-process-tree",
            },
            {
                "ts": 10.5,
                "cpu_time_s": 1.2,
                "rss_bytes": 200,
                "read_bytes": 128,
                "write_bytes": 512,
                "net_rx_bytes": 1_001_000,
                "net_tx_bytes": 2_002_000,
                "ctx_switches": 8,
                "process_count": 1,
                "available": True,
                "source": "psutil-process-tree",
            },
        ]
    )

    assert timeline[0]["net_rx_bytes_delta"] == 0
    assert timeline[0]["net_rx_bytes_per_s"] is None
    assert timeline[1]["elapsed_ms"] == 500
    assert abs(timeline[1]["cpu_time_delta_s"] - 0.2) < 0.001
    assert timeline[1]["net_rx_bytes_delta"] == 1_000
    assert timeline[1]["net_tx_bytes_delta"] == 2_000
    assert timeline[1]["net_rx_bytes_per_s"] == 2_000
    assert timeline[1]["net_tx_bytes_per_s"] == 4_000


def test_agent_test_bench_trace_jsonl_records_tool_and_model_events(tmp_path: Path) -> None:
    client, trace_dir = _trace_client(tmp_path)
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-trace-before",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-trace",
        "session_id": "session-trace",
        "session_key": None,
        "agent_id": "agent-trace",
        "tool_call_id": "call-trace",
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": "pytest",
        "derived_paths": [],
        "params_digest": "sha256:" + "c" * 64,
        "param_features": {
            "serialized_size_bytes": 24,
            "string_length": 20,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": {"command": "pytest tests/test_trace.py"},
        "raw_event": {"params": {"command": "pytest tests/test_trace.py"}},
        # Provide the current process PID so the resource sampler can capture
        # real cpu_time / rss data (needed by assertions below).  Without a
        # PID the sampler returns an empty snapshot and cpu_time_s stays None.
        "resource_scope": {"pid": os.getpid()},
    }
    decision = client.post("/v1/decisions/tool", json=request).json()

    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-trace-after",
        "occurred_at": "2026-07-16T03:23:02Z",
        "plugin_version": "0.1.0",
        "run_id": "run-trace",
        "session_id": "session-trace",
        "session_key": None,
        "agent_id": "agent-trace",
        "tool_call_id": "call-trace",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "exec",
        "duration_ms": 2000,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 128,
        "raw_result": "2 passed",
        "raw_event": {"result": "2 passed"},
    }
    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    model_started = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-model-start",
        "occurred_at": "2026-07-16T03:23:03Z",
        "plugin_version": "0.1.0",
        "run_id": "run-trace",
        "session_id": "session-trace",
        "session_key": None,
        "agent_id": "agent-trace",
        "event_type": "model_call_started",
        "call_id": "llm-trace",
        "provider": "test-provider",
        "model": "test-model",
        "duration_ms": None,
        "outcome": None,
        "context_token_budget": 8192,
        "raw_input": [{"role": "user", "content": "run tests"}],
        "raw_output": None,
        "raw_event": {"messages": [{"role": "user", "content": "run tests"}]},
    }
    model_ended = model_started | {
        "event_id": "evt-model-end",
        "occurred_at": "2026-07-16T03:23:05Z",
        "event_type": "model_call_ended",
        "duration_ms": 2000,
        "outcome": "success",
        "raw_input": None,
        "raw_output": "done",
        "raw_event": {"content": "done"},
    }
    assert client.post("/v1/events/model", json=model_started).json() == {"stored": True}
    assert client.post("/v1/events/model", json=model_ended).json() == {"stored": True}

    # Find the per-run trace file
    records = _read_trace_records(trace_dir)
    assert len(records) >= 1
    assert records[0]["record_type"] == "trace_metadata"
    assert records[0]["schema_version"] == 6

    tool_starts = [r for r in records if r.get("record_type") == "span_start" and r.get("kind") == "tool"]
    assert len(tool_starts) == 1
    tool_start = tool_starts[0]
    assert tool_start["trace_id"] == "run-trace"
    assert tool_start["agent_id"] == "agent-trace"
    assert tool_start["name"] == "exec"
    assert tool_start["input"]["requested_args"] == {"command": "pytest tests/test_trace.py"}

    tool_ends = [r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "tool"]
    assert len(tool_ends) == 1
    tool_end = tool_ends[0]
    assert tool_end["status"]["code"] == "ok"
    assert tool_end["output"]["result"] == "2 passed"
    assert tool_end["output"]["exit_code"] == 0
    assert tool_end["resources"]["cpu_time_s"] is not None
    assert tool_end["resources"]["rss_peak_bytes"] is not None
    assert "sampling_interval_ms" in tool_end["resources"]
    assert tool_end["resources"]["sampling_point_count"] >= 1
    assert isinstance(tool_end["resources"]["resource_timeline"], list)
    assert tool_end["resources"]["resource_timeline_truncated"] is False

    model_starts = [r for r in records if r.get("record_type") == "span_start" and r.get("kind") == "llm"]
    assert len(model_starts) == 1
    assert model_starts[0]["name"] == "test-model"
    assert model_starts[0]["input"]["messages"] == [{"role": "user", "content": "run tests"}]

    model_ends = [r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "llm"]
    assert len(model_ends) == 1
    assert model_ends[0]["output"]["content"] == "done"


def test_trace_marks_raw_exec_exit_code_failure(tmp_path: Path) -> None:
    client, trace_dir = _trace_client(tmp_path)
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-exec-before",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-exec-fail",
        "session_id": "session-exec-fail",
        "session_key": None,
        "agent_id": "agent-exec-fail",
        "tool_call_id": "call-exec-fail",
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": None,
        "derived_paths": [],
        "params_digest": "sha256:" + "a" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": {"command": "ls"},
        "raw_event": None,
        "resource_scope": None,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-exec-after",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-exec-fail",
        "session_id": "session-exec-fail",
        "session_key": None,
        "agent_id": "agent-exec-fail",
        "tool_call_id": "call-exec-fail",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": "call-exec-fail",
        "tool_name": "exec",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 10,
        "raw_result": {
            "details": {
                "status": "completed",
                "exitCode": 125,
                "aggregated": "Command could not be started by the execution environment.",
            }
        },
        "resource_scope": None,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    records = _read_trace_records(trace_dir)
    tool_end = next(r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "tool")
    assert tool_end["status"]["code"] == "error"
    assert tool_end["status"]["message"] == "exit_code_125"
    assert tool_end["output"]["exit_code"] == 125


def test_trace_marks_shared_runtime_process_scope(tmp_path: Path) -> None:
    client, trace_dir = _trace_client(tmp_path)
    request: dict[str, object] = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-runtime-before",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-runtime",
        "session_id": "session-runtime",
        "session_key": None,
        "agent_id": "agent-runtime",
        "tool_call_id": "call-runtime",
        "tool_name": "write",
        "tool_kind": "internal",
        "tool_input_kind": "json",
        "operation_hint": None,
        "derived_paths": [],
        "params_digest": "sha256:" + "d" * 64,
        "param_features": {
            "serialized_size_bytes": 24,
            "string_length": 20,
            "list_item_count": 0,
            "path_count": 1,
            "has_command_like_field": False,
        },
        "raw_params": {"path": "x.txt"},
        "resource_scope": {
            "kind": "pid",
            "pid": os.getpid(),
            "root_pid": os.getpid(),
            "process_start_time": None,
            "root_starttime_ticks": None,
            "cgroup_path": None,
            "pid_namespace_inode": None,
            "container_id": None,
            "include_children": True,
            "source": "openclaw-runtime",
            "attribution_source": "shared-runtime-process",
        },
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-runtime-after",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-runtime",
        "session_id": "session-runtime",
        "session_key": None,
        "agent_id": "agent-runtime",
        "tool_call_id": "call-runtime",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "write",
        "duration_ms": 1000,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 2,
        "raw_result": "ok",
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    records = _read_trace_records(trace_dir)
    tool_end = next(r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "tool")
    assert tool_end["execution"]["payload_pid"] == os.getpid()
    assert tool_end["resources"]["attribution_status"] == "partially_attributed"
    assert tool_end["resources"]["coverage_reason"] in {
        "shared_runtime_process",
        "monitor_window_no_overlap",
    }


def test_proxy_capture_without_model_hook_does_not_write_standalone_trace(tmp_path: Path, monkeypatch) -> None:
    client, trace_dir = _trace_proxy_client(tmp_path)
    # Remove any existing trace files
    for f in trace_dir.glob("*.jsonl"):
        f.unlink()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url, headers=None, content=None):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "world"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

    monkeypatch.setattr("agent_scheduler.llm_proxy.httpx.AsyncClient", FakeAsyncClient)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert _read_trace_records(trace_dir) == []


def test_llm_proxy_records_full_request_and_response(tmp_path: Path, monkeypatch) -> None:
    client, trace_dir = _trace_proxy_client(tmp_path)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url, headers=None, content=None):
            assert url == "https://upstream.example/v1/chat/completions"
            request_payload = json.loads(content.decode("utf-8"))
            assert request_payload["messages"][0]["content"] == "hello"
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": request_payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "world"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

    monkeypatch.setattr("agent_scheduler.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "world"
    # Proxy-only calls should not write trace without model hook
    assert _read_trace_records(trace_dir) == []


def test_model_hook_record_is_enriched_from_proxy_capture(tmp_path: Path, monkeypatch) -> None:
    client, trace_dir = _trace_proxy_client(tmp_path)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url, headers=None, content=None):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "world",
                                "tool_calls": [
                                    {
                                        "id": "call-proxy-tool",
                                        "type": "function",
                                        "function": {"name": "exec", "arguments": '{"command":"pwd"}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            )

    monkeypatch.setattr("agent_scheduler.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    assert client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    ).status_code == 200

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-model-start-proxy",
        "occurred_at": now,
        "plugin_version": "0.1.0",
        "run_id": "run-proxy",
        "session_id": "session-proxy",
        "session_key": "agent:main:main",
        "agent_id": None,
        "event_type": "model_call_started",
        "call_id": "run-proxy:model:1",
        "provider": "vllm",
        "model": "test-model",
        "duration_ms": None,
        "outcome": None,
        "context_token_budget": 8192,
        "raw_input": None,
        "raw_output": None,
        "raw_event": {"runId": "run-proxy", "sessionId": "session-proxy"},
    }
    ended = started | {
        "event_id": "evt-model-end-proxy",
        "occurred_at": now,
        "event_type": "model_call_ended",
        "duration_ms": 2000,
        "outcome": "completed",
        "raw_event": {"runId": "run-proxy", "sessionId": "session-proxy"},
    }
    assert client.post("/v1/events/model", json=started).json() == {"stored": True}
    assert client.post("/v1/events/model", json=ended).json() == {"stored": True}

    records = _read_trace_records(trace_dir)
    llm_starts = [r for r in records if r.get("record_type") == "span_start" and r.get("kind") == "llm"]
    assert len(llm_starts) == 1
    assert llm_starts[0]["run_id"] == "run-proxy"
    assert llm_starts[0]["session_id"] == "session-proxy"
    assert llm_starts[0]["agent_id"] == "main"
    assert llm_starts[0]["input"]["messages"] == [{"role": "user", "content": "hello"}]
    llm_ends = [r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "llm"]
    assert llm_ends[0]["output"]["content"]["content"] == "world"
    assert llm_ends[0]["output"]["content"]["tool_calls"][0]["id"] == "call-proxy-tool"


def test_llm_proxy_reconstructs_streaming_tool_calls(tmp_path: Path, monkeypatch) -> None:
    client, trace_dir = _trace_proxy_client(tmp_path)

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def aiter_bytes(self):
            chunks = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "exec", "arguments": '{"command":"py'},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": 'thon --version"}'}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ]
            for chunk in chunks:
                yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method, url, headers=None, content=None):
            return FakeStream()

    monkeypatch.setattr("agent_scheduler.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "run python"}],
        },
    )

    assert response.status_code == 200
    # Proxy-only streaming should not write trace without model hook
    assert _read_trace_records(trace_dir) == []
    assert list(trace_dir.glob("llm_proxy_debug_*.json")) == []


def test_llm_proxy_buffers_fragmented_sse_events(tmp_path: Path, monkeypatch) -> None:
    client, _trace_dir = _trace_proxy_client(tmp_path)
    event = {
        "choices": [
            {
                "index": 0,
                "delta": {"content": "hello"},
                "finish_reason": None,
            }
        ]
    }
    wire = f"data: {json.dumps(event)}\n\n".encode("utf-8")

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def aiter_bytes(self):
            yield wire[:25]
            yield wire[25:]
            yield b"data: [DONE]\n\n"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method, url, headers=None, content=None):
            return FakeStream()

    monkeypatch.setattr("agent_scheduler.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert response.text.count("data: ") == 2
    assert '"content": "hello"' in response.text
    assert "[DONE]" in response.text


def test_llm_proxy_writes_debug_dump_only_when_enabled(tmp_path: Path, monkeypatch) -> None:
    client, trace_dir = _trace_proxy_client_with_debug(tmp_path)
    event = {
        "choices": [
            {
                "index": 0,
                "delta": {"content": "hello"},
                "finish_reason": "stop",
            }
        ]
    }

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def aiter_bytes(self):
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method, url, headers=None, content=None):
            return FakeStream()

    monkeypatch.setattr("agent_scheduler.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    debug_files = list(trace_dir.glob("llm_proxy_debug_*.json"))
    assert len(debug_files) == 1


def test_execution_registration_round_trip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/v2/executions",
        json={
            "execution_id": "exec-1",
            "tool_call_id": "call-1",
            "run_id": "run-1",
            "session_key_hash": "sha256:" + "a" * 64,
            "command_digest": "sha256:" + "b" * 64,
            "command": "pytest tests -q",
            "workdir": "/workspace",
            "host": "gateway",
            "placement": {"cpu_set": None, "numa_node": None, "llc_cluster": None, "advisory": True},
            "profiling": {"mode": "off"},
            "backend": "marker",
        },
    )
    assert response.status_code == 200
    registration = response.json()
    assert registration["execution_id"] == "exec-1"
    assert registration["one_time_token"]
    assert registration["expires_at"].endswith("Z")

    claim = client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "exec-1",
            "token": registration["one_time_token"],
            "launcher_pid": 100,
        },
    )
    assert claim.status_code == 200
    spec = claim.json()
    assert spec["execution_id"] == "exec-1"
    assert spec["command"] == "pytest tests -q"
    assert spec["workdir"] == "/workspace"
    assert spec["update_token"]

    duplicate_claim = client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "exec-1",
            "token": registration["one_time_token"],
            "launcher_pid": 100,
        },
    )
    assert duplicate_claim.status_code == 409

    started = client.post(
        "/v2/executions/exec-1/started",
        json={
            "update_token": spec["update_token"],
            "launcher_pid": 100,
            "child_pid": 101,
            "process_starttime_ticks": 12345,
            "cgroup_path": None,
            "pid_namespace_inode": 4026531836,
            "container_id": None,
        },
    )
    assert started.status_code == 200
    assert started.json() == {"stored": True}

    scope = client.get("/v2/executions/exec-1/scope")
    assert scope.status_code == 200
    assert scope.json()["execution_scope"] == {
        "kind": "pid",
        "execution_id": "exec-1",
        "pid": 101,
        "root_pid": 101,
        "process_start_time": None,
        "root_starttime_ticks": 12345.0,
        "cgroup_path": None,
        "pid_namespace_inode": 4026531836,
        "container_id": None,
        "include_children": True,
        "source": "claw-launch",
        "attribution_source": "claw-launch",
    }

    exited = client.post(
        "/v2/executions/exec-1/exited",
        json={"update_token": spec["update_token"], "exit_code": 0, "signal": None},
    )
    assert exited.status_code == 200
    assert exited.json() == {"stored": True}
