from __future__ import annotations

import json
import os
import time
from threading import Event
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import clawtune_sidecar.api.app as app_module
from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.contracts.models import ParamFeatures, ResourceScope, ToolBeforeRequest
from clawtune_sidecar.llm_proxy import (
    _forward_headers,
    _parse_sse_buffer,
    _upstream_url,
)
from clawtune_sidecar.monitoring.docker_exec import DockerExecObserver, _docker_events_command
from clawtune_sidecar.monitoring.tool_runtime import _relative_timeline
from clawtune_sidecar.trace import _coverage, _tool_timestamps


def test_tool_action_window_uses_reported_duration_not_monitor_window() -> None:
    sample = SimpleNamespace(started_at=100.0, ended_at=101.0)

    assert _tool_timestamps(sample, 100) == (100.9, 101.0)


def test_coverage_ratio_is_defensively_bounded() -> None:
    duration_ns, ratio, reason = _coverage(
        action_start_wall_ns=1_000,
        action_end_wall_ns=2_000,
        action_duration_ns=100,
        monitor_start_wall_ns=900,
        monitor_end_wall_ns=2_100,
        has_pid=True,
    )

    assert duration_ns == 100
    assert ratio == 1.0
    assert reason == "full_window"


def test_v6_quality_is_honest_about_partial_sampling() -> None:
    from clawtune_sidecar.trace import _v6_quality

    # Full window coverage with healthy sampling is the only "complete".
    assert _v6_quality("ok", "full_window") == "complete"
    # Partial/low sampling must never be labelled complete even under full
    # coverage (e.g. a rebased/mixed baseline that only sampled a late window).
    assert _v6_quality("partial", "full_window") == "partial"
    assert _v6_quality("low", "full_window") == "partial"
    assert _v6_quality("partial", "pid_registered_late") == "partial"
    assert _v6_quality("ok", "pid_registered_late") == "partial"
    assert _v6_quality("unattributed", "full_window") == "unknown"
    assert _v6_quality("unavailable", "full_window") == "unknown"


def test_host_execution_cgroup_roots_are_permission_aware(monkeypatch) -> None:
    from clawtune_sidecar.api import app as app_module

    fallback = ResourceScope(
        kind="cgroup-v2",
        pid=1,
        root_pid=1,
        cgroup_path="/sys/fs/cgroup/system.slice/docker-x.scope",
        source="openclaw-sandbox",
        attribution_source="shared-sandbox-container",
    )
    # Non-root (no geteuid on this platform -> euid -1): only the fallback
    # container subtree candidate is emitted.
    roots = app_module._host_execution_cgroup_roots(fallback, None)
    assert "/sys/fs/cgroup/system.slice/docker-x.scope/clawtune-executions" in roots

    # A configured root short-circuits the search entirely.
    assert app_module._host_execution_cgroup_roots(fallback, "/cfg/clawtune") == [
        "/cfg/clawtune"
    ]

    # Root (euid 0): the sandbox container subtree is tried FIRST (a child of
    # the payload's cgroup inherits the same controllers, so the move is the
    # most reliable), then the root-managed clawtune cgroup.
    import types

    monkeypatch.setattr(
        app_module, "os", types.SimpleNamespace(geteuid=lambda: 0)
    )
    roots_root = app_module._host_execution_cgroup_roots(fallback, None)
    assert roots_root[0].replace("\\", "/") == (
        "/sys/fs/cgroup/system.slice/docker-x.scope/clawtune-executions"
    )
    assert any(
        root.replace("\\", "/") == "/sys/fs/cgroup/clawtune"
        for root in roots_root
    )


def test_docker_exec_observer_cgroup_diff_fallback_captures_pid(tmp_path: Path) -> None:
    from clawtune_sidecar.contracts.models import ToolCompletedEvent

    cgroup = tmp_path / "sandbox-cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("100\n", encoding="utf-8")
    request = ToolBeforeRequest(
        schema_version="clawtune.v1",
        event_id="evt-read-start",
        occurred_at="2026-07-16T03:23:00Z",
        plugin_version="0.1.0",
        run_id="run-diff",
        session_id="session-diff",
        session_key=None,
        agent_id=None,
        tool_call_id="call-read",
        tool_name="read",
        tool_kind="file",
        tool_input_kind="json",
        operation_hint=None,
        derived_paths=[],
        params_digest="sha256:" + "a" * 64,
        param_features=ParamFeatures(
            serialized_size_bytes=10,
            string_length=5,
            list_item_count=0,
            path_count=1,
            has_command_like_field=False,
        ),
        raw_params={"path": "README.md"},
        resource_scope=ResourceScope(
            kind="cgroup-v2",
            cgroup_path=str(cgroup),
            container_id="sandbox-1",
            include_children=True,
            source="openclaw-sandbox",
            attribution_source="shared-sandbox-container",
        ),
    )
    observer = DockerExecObserver(
        enabled=True, cgroup_path=str(cgroup), autostart=False
    )
    observer.begin_tool(request)
    # A new pid (the read docker exec) appears in the container cgroup during
    # the tool window; the docker-events path finds nothing.
    (cgroup / "cgroup.procs").write_text("100\n12345\n", encoding="utf-8")
    observer._poll_cgroup_once()
    event = ToolCompletedEvent(
        schema_version="clawtune.v1",
        event_id="evt-read-end",
        occurred_at="2026-07-16T03:23:01Z",
        plugin_version="0.1.0",
        run_id="run-diff",
        session_id="session-diff",
        session_key=None,
        agent_id=None,
        tool_call_id="call-read",
        decision_id="decision-1",
        lease_id="lease-1",
        execution_id=None,
        tool_name="read",
        duration_ms=100,
        succeeded=True,
        error_type=None,
        error_digest=None,
        result_size_bytes=4,
        raw_result="data",
        resource_scope=None,
    )
    scope = observer.infer_scope(event)
    assert scope is not None
    assert scope.kind == "pid"
    assert scope.root_pid == 12345
    assert scope.attribution_source == "docker-exec-pid"
    assert scope.source == "docker-cgroup-diff"
    diag = observer.diagnostics()
    assert diag["cgroup_diff_captures"] == 1


def test_prepare_host_execution_cgroup_moves_pid_tree_and_records_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    (cgroup_root / "cgroup.controllers").write_text(
        "cpu memory\n", encoding="utf-8"
    )
    monkeypatch.setattr(app_module, "_CGROUP_V2_ROOT", cgroup_root)
    monkeypatch.setattr(app_module, "_resolve_host_pid", lambda *_args, **_kwargs: 4242)
    # The payload has already forked a child; both must be moved into the
    # per-execution cgroup so the cgroup captures the whole tool's CPU.
    monkeypatch.setattr(
        app_module, "_process_tree_pids", lambda _pid: [4242, 9999]
    )
    request = SimpleNamespace(
        child_pid=7,
        pid_namespace_inode=123,
        process_starttime_ticks=456,
        container_id=None,
    )
    diagnostics: list[str] = []

    scope = app_module._prepare_host_execution_cgroup(
        "exec-1",
        request,
        None,
        None,
        diagnostics=diagnostics,
    )

    assert scope is not None
    assert scope.kind == "cgroup-v2"
    assert scope.cgroup_path == str(cgroup_root / "clawtune" / "exec-1")
    assert scope.attribution_source == "exclusive-execution-cgroup"
    procs = (cgroup_root / "clawtune" / "exec-1" / "cgroup.procs").read_text(
        encoding="utf-8"
    ).split()
    assert "4242" in procs
    assert "9999" in procs
    joined = "\n".join(diagnostics)
    assert "resolved launcher host pid 4242" in joined
    assert "owns launcher pid tree" in joined


def test_cgroup_accounting_usable_reads_subtree_control(tmp_path) -> None:
    from clawtune_sidecar.api import app as app_module

    parent = tmp_path / "cg"
    parent.mkdir()
    (parent / "cgroup.subtree_control").write_text("cpu memory", encoding="utf-8")
    assert app_module._cgroup_accounting_usable(parent) is True

    (parent / "cgroup.subtree_control").write_text("pids", encoding="utf-8")
    assert app_module._cgroup_accounting_usable(parent) is False


def _read_trace_records(trace_dir: Path) -> list[dict]:
    """Find the first JSONL file in trace_dir and return parsed records."""
    files = list(trace_dir.glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def _client(tmp_path: Path) -> TestClient:
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    return TestClient(create_app(state))


def _trace_client(tmp_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(SidecarConfig(trace_dir=trace_dir))
    return TestClient(create_app(state)), trace_dir


def _trace_proxy_client(tmp_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            llm_proxy_upstream_base_url="https://upstream.example/v1",
        )
    )
    return TestClient(create_app(state)), trace_dir


def _trace_client_with_sandbox_cgroup(tmp_path: Path, cgroup_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            sandbox_cgroup_path=str(cgroup_path),
            sandbox_container_id="sandbox-1",
            tool_resource_ebpf_required=False,
        )
    )
    return TestClient(create_app(state)), trace_dir


def test_health_endpoints_publish_stable_clawtune_identity(tmp_path: Path) -> None:
    client = _client(tmp_path)

    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    live_payload = live.json()
    assert live_payload["schema_version"] == "clawtune.health.v1"
    assert live_payload["service"] == "clawtune-sidecar"
    assert live_payload["live"] is True
    # Version-negotiation fields are part of the initial 0.1.0 protocol.
    assert isinstance(live_payload.get("sidecar_version"), str)
    assert isinstance(live_payload.get("protocol_versions"), list)

    assert ready.status_code == 200
    ready_payload = ready.json()
    assert ready_payload["schema_version"] == "clawtune.health.v1"
    assert ready_payload["service"] == "clawtune-sidecar"
    assert ready_payload["ready"] is True
    assert isinstance(ready_payload.get("sidecar_version"), str)
    assert isinstance(ready_payload.get("protocol_versions"), list)


def _write_cgroup_fixture(path: Path, usage_usec: int = 100_000) -> None:
    path.mkdir()
    (path / "cpu.stat").write_text(f"usage_usec {usage_usec}\n", encoding="utf-8")
    (path / "memory.current").write_text("4096\n", encoding="utf-8")
    (path / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (path / "cgroup.procs").write_text("", encoding="utf-8")


def test_host_cgroup_path_is_derived_from_verified_pid_membership(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    target = cgroup_root / "user.slice" / "tool.scope"
    (proc_root / "4242").mkdir(parents=True)
    target.mkdir(parents=True)
    (cgroup_root / "cgroup.controllers").write_text("cpu memory\n", encoding="utf-8")
    (proc_root / "4242" / "cgroup").write_text(
        "0::/user.slice/tool.scope\n",
        encoding="utf-8",
    )
    (target / "cgroup.procs").write_text("4242\n", encoding="utf-8")

    assert app_module._host_cgroup_path_for_pid(
        4242,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
    ) == str(target.resolve())


@pytest.mark.parametrize(
    ("relative", "members"),
    [
        ("/", "4242\n"),
        ("/user.slice/tool.scope", "9999\n"),
        ("/../outside", "4242\n"),
    ],
)
def test_host_cgroup_path_rejects_root_nonmember_and_escape(
    tmp_path: Path,
    relative: str,
    members: str,
) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    target = cgroup_root / "user.slice" / "tool.scope"
    (proc_root / "4242").mkdir(parents=True)
    target.mkdir(parents=True)
    (cgroup_root / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (proc_root / "4242" / "cgroup").write_text(
        f"0::{relative}\n",
        encoding="utf-8",
    )
    (target / "cgroup.procs").write_text(members, encoding="utf-8")

    assert app_module._host_cgroup_path_for_pid(
        4242,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
    ) is None


def test_host_cgroup_gate_uses_standard_v2_root_when_unconfigured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    (cgroup_root / "cgroup.controllers").write_text("cpu memory\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "_CGROUP_V2_ROOT", cgroup_root)
    monkeypatch.setattr(app_module, "_resolve_host_pid", lambda *_args, **_kwargs: 4242)
    request = SimpleNamespace(
        child_pid=7,
        pid_namespace_inode=123,
        process_starttime_ticks=456,
        container_id=None,
    )

    scope = app_module._prepare_host_execution_cgroup(
        "exec-1",
        request,
        None,
        None,
    )

    assert scope is not None
    assert scope.cgroup_path == str(cgroup_root / "clawtune" / "exec-1")
    assert (
        cgroup_root / "clawtune" / "exec-1" / "cgroup.procs"
    ).read_text(encoding="utf-8").strip() == "4242"


def test_verified_host_scope_falls_back_to_authenticated_pid_lineage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shared = tmp_path / "session.scope"
    shared.mkdir()
    monkeypatch.setattr(
        app_module,
        "_host_cgroup_path_for_pid",
        lambda _pid: str(shared),
    )
    monkeypatch.setattr(app_module, "_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(app_module, "_pid_namespace_inode", lambda _pid: 123)

    scope = app_module._verified_host_execution_scope(
        "exec-1",
        SimpleNamespace(cgroup_path=None),
        4242,
    )

    assert scope is not None
    assert scope.root_pid == 4242
    assert scope.cgroup_path == str(shared)
    assert scope.attribution_source == "trusted-execution-root-pid"


def _trace_proxy_client_with_debug(tmp_path: Path) -> tuple[TestClient, Path]:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            llm_proxy_upstream_base_url="https://upstream.example/v1",
            llm_proxy_debug_dump=True,
        )
    )
    return TestClient(create_app(state)), trace_dir


def test_llm_proxy_upstream_url_preserves_v1_when_base_omits_it() -> None:
    assert (
        _upstream_url(
            SidecarConfig(llm_proxy_upstream_base_url="https://api.deepseek.com"),
            "/v1/chat/completions",
        )
        == "https://api.deepseek.com/v1/chat/completions"
    )
    assert (
        _upstream_url(
            SidecarConfig(llm_proxy_upstream_base_url="https://api.deepseek.com/v1"),
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

    headers_without_upstream_key = _forward_headers(Request(), SidecarConfig())
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
        SidecarConfig(llm_proxy_upstream_api_key="real-key"),
    )

    assert headers["authorization"] == "Bearer real-key"
    assert headers["content-type"] == "application/json"


def test_decision_and_completion_round_trip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    request: dict[str, object] = {
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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


def test_internal_tool_prefers_shared_sandbox_over_shared_runtime_scope(
    tmp_path: Path,
) -> None:
    cgroup = tmp_path / "cgroup"
    runtime_cgroup = tmp_path / "runtime-cgroup"
    cgroup.mkdir()
    runtime_cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 100000\n", encoding="utf-8")
    (cgroup / "memory.current").write_text("4096\n", encoding="utf-8")
    (cgroup / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    (runtime_cgroup / "cpu.stat").write_text("usage_usec 900000\n", encoding="utf-8")
    (runtime_cgroup / "memory.current").write_text("8192\n", encoding="utf-8")
    (runtime_cgroup / "io.stat").write_text(
        "8:0 rbytes=100 wbytes=200\n", encoding="utf-8"
    )
    (runtime_cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    client, trace_dir = _trace_client_with_sandbox_cgroup(tmp_path, cgroup)
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
        "schema_version": "clawtune.v1",
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
        "resource_scope": shared_runtime_scope,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    (cgroup / "cpu.stat").write_text("usage_usec 200000\n", encoding="utf-8")
    completion = {
        "schema_version": "clawtune.v1",
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
        "resource_scope": shared_runtime_scope,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    trace_records = _read_trace_records(trace_dir)
    tool_start = [
        record
        for record in trace_records
        if record.get("record_type") == "span_start" and record.get("kind") == "tool"
    ][0]
    tool_end = [
        record
        for record in trace_records
        if record.get("record_type") == "span_end" and record.get("kind") == "tool"
    ][0]
    assert tool_end["execution"]["cgroup_path"] == str(cgroup)
    assert tool_end["resources"]["attribution_status"] == "partially_attributed"
    assert tool_end["resources"]["scope"] == "cgroup"
    assert tool_end["resources"]["coverage_reason"] == "shared_sandbox_container"
    assert tool_end["resources"]["monitor_duration_ns"] is not None
    assert tool_end["resources"]["cgroup_cpu_time_s"] is not None
    assert (
        int(tool_end["monotonic_time_ns"]) - int(tool_start["monotonic_time_ns"])
        == int(tool_end["duration_ns"])
    )


def test_internal_tool_uses_docker_exec_inferred_scope_before_fallback(tmp_path: Path) -> None:
    fallback_cgroup = tmp_path / "fallback-cgroup"
    inferred_cgroup = tmp_path / "inferred-cgroup"
    _write_cgroup_fixture(fallback_cgroup, usage_usec=100_000)
    _write_cgroup_fixture(inferred_cgroup, usage_usec=500_000)
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
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
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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
    assert tool_end["execution"]["cgroup_path"] is None
    assert tool_end["execution"]["payload_pid"] == os.getpid()
    assert tool_end["execution"]["source"] == "docker-events"
    assert tool_end["resources"]["scope"] == "process_tree"
    assert tool_end["resources"]["attribution_source"] == "docker-exec-pid"
    assert tool_end["resources"]["attribution_status"] == "attributed"
    assert tool_end["resources"]["coverage_reason"] != "shared_sandbox_container"


def test_internal_tool_overrides_shared_runtime_scope_with_docker_exec(tmp_path: Path) -> None:
    runtime_cgroup = tmp_path / "runtime-cgroup"
    inferred_cgroup = tmp_path / "inferred-cgroup"
    _write_cgroup_fixture(runtime_cgroup, usage_usec=100_000)
    _write_cgroup_fixture(inferred_cgroup, usage_usec=500_000)
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
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
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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
    assert tool_end["execution"]["cgroup_path"] is None
    assert tool_end["execution"]["payload_pid"] == os.getpid()
    assert tool_end["execution"]["source"] == "docker-events"
    assert tool_end["resources"]["scope"] == "process_tree"
    assert tool_end["resources"]["attribution_source"] == "docker-exec-pid"
    assert tool_end["resources"]["coverage_reason"] != "shared_runtime_process"


def test_host_openclaw_scoped_read_gets_docker_exec_pid(tmp_path: Path) -> None:
    """host-openclaw-sandbox: read/edit run as docker execs with a per-tool PID.

    ``with_sandbox_fallback`` gives the before-request an openclaw-sandbox
    (shared-sandbox-container) scope, but the Docker observer must still
    register the tool and override that whole-container scope with the
    per-tool docker-exec PID captured from the sandbox's ``docker exec``.
    """
    sandbox_cgroup = tmp_path / "sandbox-cgroup"
    _write_cgroup_fixture(sandbox_cgroup, usage_usec=100_000)
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            sandbox_cgroup_path=str(sandbox_cgroup),
            sandbox_container_id="sandbox-1",
        )
    )
    state.docker_exec_observer = DockerExecObserver(
        enabled=True,
        container_id="sandbox-1",
        autostart=False,
    )
    client = TestClient(create_app(state))
    sandbox_scope: dict[str, object] = {
        "kind": "cgroup-v2",
        "execution_id": None,
        "pid": os.getpid(),
        "root_pid": os.getpid(),
        "process_start_time": None,
        "root_starttime_ticks": None,
        "cgroup_path": str(sandbox_cgroup),
        "pid_namespace_inode": None,
        "container_id": "sandbox-1",
        "include_children": True,
        "source": "openclaw-sandbox",
        "attribution_source": "shared-sandbox-container",
    }
    request: dict[str, object] = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-read-sandbox-start",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-docker-exec",
        "session_id": "session-docker-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read-sandbox",
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
        "resource_scope": sandbox_scope,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    state.docker_exec_observer.record_exec_start(
        exec_id="exec-read-sandbox-1",
        container_id="sandbox-1",
        pid=os.getpid(),
        command="openclaw-sandbox-fs read README.md",
    )
    completion: dict[str, object] = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-read-sandbox-end",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-docker-exec",
        "session_id": "session-docker-exec",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-read-sandbox",
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
        "resource_scope": sandbox_scope,
    }

    assert client.post("/v1/events/tool-completed", json=completion).json() == {
        "stored": True
    }

    tool_end = [
        record
        for record in _read_trace_records(trace_dir)
        if record.get("record_type") == "span_end" and record.get("kind") == "tool"
    ][0]
    assert tool_end["execution"]["source"] == "docker-events"
    assert tool_end["execution"]["payload_pid"] == os.getpid()
    assert tool_end["resources"]["scope"] == "process_tree"
    assert tool_end["resources"]["attribution_source"] == "docker-exec-pid"
    assert tool_end["resources"]["attribution_status"] == "attributed"
    assert tool_end["resources"]["coverage_reason"] != "shared_sandbox_container"


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
                        "name": "clawtune-srb-test-1",
                    },
                },
            }
        )
    )

    assert inspected == ["exec-real-id"]
    assert observer._records[0].exec_id == "exec-real-id"


def test_docker_exec_observer_binds_live_pid_before_completion() -> None:
    bound = []
    observer = DockerExecObserver(
        enabled=True,
        container_id="sandbox-1",
        autostart=False,
        on_scope=lambda tool_call_id, scope: (
            bound.append((tool_call_id, scope)) or True
        ),
    )
    observer.begin_tool(
        ToolBeforeRequest(
            schema_version="clawtune.v1",
            event_id="evt-read",
            occurred_at="2026-07-16T03:23:00Z",
            plugin_version="0.1.0",
            run_id="run-1",
            session_id="session-1",
            session_key=None,
            agent_id=None,
            tool_call_id="call-read",
            tool_name="read",
            tool_kind="file",
            tool_input_kind="json",
            derived_paths=[],
            params_digest="sha256:" + "a" * 64,
            param_features=ParamFeatures(
                serialized_size_bytes=1,
                string_length=1,
                list_item_count=0,
                path_count=1,
                has_command_like_field=False,
            ),
        )
    )

    observer.record_exec_start(
        exec_id="exec-read",
        container_id="sandbox-1",
        pid=os.getpid(),
        command="openclaw-sandbox-fs read README.md",
    )

    assert bound[0][0] == "call-read"
    assert bound[0][1].kind == "pid"
    assert bound[0][1].pid == os.getpid()
    assert bound[0][1].attribution_source == "docker-exec-pid"


def test_docker_exec_observer_subscribes_before_container_exec_start() -> None:
    command = _docker_events_command("docker")

    assert command == [
        "docker",
        "events",
        "--format",
        "{{json .}}",
        "--filter",
        "type=container",
        "--filter",
        "event=exec_create",
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
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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


@pytest.mark.parametrize("launcher_cgroup_path", ["/sys/fs/cgroup", None])
def test_exec_unusable_launcher_scope_falls_back_to_shared_sandbox(
    tmp_path: Path,
    launcher_cgroup_path: str | None,
) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 100000\n", encoding="utf-8")
    (cgroup / "memory.current").write_text("4096\n", encoding="utf-8")
    (cgroup / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    client, trace_dir = _trace_client_with_sandbox_cgroup(tmp_path, cgroup)
    request: dict[str, object] = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-exec-start",
        "occurred_at": "2026-07-16T03:23:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-sandbox-root-exec",
        "session_id": "session-sandbox-root-exec",
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
            "run_id": "run-sandbox-root-exec",
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
            # A container PID can collide with a real host PID.  It must not
            # replace the host-side sandbox cgroup sampler merely because the
            # numeric PID happens to be readable on the sidecar host.
            "child_pid": os.getpid(),
            "process_starttime_ticks": 123,
            "cgroup_path": launcher_cgroup_path,
            "pid_namespace_inode": 456,
            "container_id": "sandbox-1",
        },
    )
    (cgroup / "cpu.stat").write_text("usage_usec 200000\n", encoding="utf-8")
    completion = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-exec-end",
        "occurred_at": "2026-07-16T03:23:01Z",
        "plugin_version": "0.1.0",
        "run_id": "run-sandbox-root-exec",
        "session_id": "session-sandbox-root-exec",
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
    assert tool_end["execution"]["cgroup_path"] == str(cgroup)
    assert tool_end["resources"]["attribution_source"] == "shared-sandbox-container"
    assert tool_end["resources"]["attribution_status"] == "partially_attributed"
    assert tool_end["resources"]["coverage_reason"] == "shared_sandbox_container"


def test_ebpf_execution_waits_for_sandbox_container_scope(tmp_path: Path) -> None:
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    begin_calls: list[dict[str, object]] = []
    started_execution_ids: set[str] = set()

    def begin_execution(**kwargs):
        if kwargs["execution_id"] in started_execution_ids:
            return False
        started_execution_ids.add(kwargs["execution_id"])
        begin_calls.append(kwargs)
        return True

    state.predictor.begin_execution = begin_execution  # type: ignore[method-assign]

    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-launcher-exec",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi && true",
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
    assert begin_calls == []

    client.post(
        "/v1/runtime/sandbox-scope",
        json={
            "kind": "cgroup-v2",
            "execution_id": None,
            "pid": os.getpid(),
            "root_pid": os.getpid(),
            "process_start_time": None,
            "root_starttime_ticks": None,
            "cgroup_path": str(tmp_path / "sandbox-cgroup"),
            "pid_namespace_inode": None,
            "container_id": "b" * 64,
            "include_children": True,
            "source": "openclaw-sandbox",
            "attribution_source": "shared-sandbox-container",
        },
    )
    client.post(
        "/v2/executions/call-exec/started",
        json={
            "update_token": claim["update_token"],
            "launcher_pid": os.getpid(),
            "child_pid": os.getpid(),
            "process_starttime_ticks": 123,
            "cgroup_path": str(tmp_path / "call-cgroup"),
            "pid_namespace_inode": 456,
            "container_id": None,
        },
    )

    assert begin_calls == [
        {
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "command": "echo hi && true",
            "container_id": "b" * 64,
            "repo": "openclaw",
        }
    ]


def test_ebpf_execution_starts_on_claim_when_sandbox_scope_is_known(tmp_path: Path) -> None:
    sandbox_cgroup = tmp_path / "sandbox-cgroup"
    _write_cgroup_fixture(sandbox_cgroup)
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            sandbox_cgroup_path=str(sandbox_cgroup),
            sandbox_container_id="b" * 64,
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    begin_calls: list[dict[str, object]] = []

    def begin_execution(**kwargs):
        begin_calls.append(kwargs)
        return True

    state.predictor.begin_execution = begin_execution  # type: ignore[method-assign]

    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-launcher-exec",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi && true",
            "workdir": "/workspace",
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
            "backend": "managed-wrapper",
        },
    ).json()
    client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "call-exec",
            "token": registration["one_time_token"],
            "launcher_pid": os.getpid(),
        },
    )

    assert begin_calls == [
        {
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "command": "echo hi && true",
            "container_id": "b" * 64,
            "repo": "openclaw",
        }
    ]


def test_execution_exit_waits_for_completion_result_before_ebpf_finish(
    tmp_path: Path,
) -> None:
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    finish_calls: list[dict[str, object]] = []

    def finish_execution(**kwargs):
        finish_calls.append(kwargs)
        return {
            "execution_id": kwargs["execution_id"],
            "tool_call_id": "call-result",
            "artifact_path": None,
            "started": True,
            "status": "ok",
            "unavailable_reason": None,
        }

    state.predictor.finish_execution = finish_execution  # type: ignore[method-assign]
    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "exec-result",
            "tool_call_id": "call-result",
            "run_id": "run-result",
            "session_key_hash": None,
            "command_digest": "sha256:" + "d" * 64,
            "command": "pip install -e . 2>&1 | tail -10",
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
            "execution_id": "exec-result",
            "token": registration["one_time_token"],
            "launcher_pid": 100,
        },
    ).json()

    assert client.post(
        "/v2/executions/exec-result/exited",
        json={
            "update_token": claim["update_token"],
            "exit_code": 0,
            "signal": None,
        },
    ).json() == {"stored": True}
    assert finish_calls == []
    # Telemetry reads must never acquire/finalize the active eBPF run.
    client.get("/v2/executions/exec-result/telemetry")
    assert finish_calls == []

    original_scope = state.executions.scope

    def scope_after_ebpf_finish(execution_id: str):
        # Completion finalization must happen before the route's async scope
        # lookup, otherwise an 800 ms plugin timeout can race a raw-less GET.
        assert finish_calls
        return original_scope(execution_id)

    state.executions.scope = scope_after_ebpf_finish  # type: ignore[method-assign]

    completion = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-result",
        "occurred_at": "2026-07-29T00:00:00Z",
        "plugin_version": "0.1.0",
        "run_id": "run-result",
        "session_id": "session-result",
        "session_key": None,
        "agent_id": None,
        "tool_call_id": "call-result",
        "decision_id": None,
        "lease_id": None,
        "execution_id": "exec-result",
        "tool_name": "exec",
        "duration_ms": 100,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": None,
        "raw_result": {
            "details": {
                "exitCode": 0,
                "aggregated": "/bin/sh: 1: pip: not found",
            }
        },
        "resource_scope": None,
    }
    assert client.post("/v1/events/tool-completed", json=completion).json() == {
        "stored": True
    }
    assert finish_calls == [
        {
            "execution_id": "exec-result",
            "exit_code": 0,
            "signal": None,
            "raw_result": completion["raw_result"],
            "succeeded": True,
        }
    ]


def test_running_exec_completion_waits_for_real_exit_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sandbox_cgroup = tmp_path / "sandbox-cgroup"
    _write_cgroup_fixture(sandbox_cgroup)
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            sandbox_cgroup_path=str(sandbox_cgroup),
            sandbox_container_id="b" * 64,
            tool_resource_ebpf_required=False,
        )
    )
    monkeypatch.setattr(app_module, "_EBPF_COMPLETION_GRACE_SECONDS", 0.01)
    finish_calls: list[dict[str, object]] = []
    finalized = Event()

    def finish_execution(**kwargs):
        finish_calls.append(kwargs)
        finalized.set()
        return {
            "execution_id": kwargs["execution_id"],
            "tool_call_id": kwargs["execution_id"],
            "artifact_path": None,
            "started": True,
            "status": "ok",
            "unavailable_reason": None,
        }

    state.predictor.begin_execution = lambda **_kwargs: True  # type: ignore[method-assign]
    state.predictor.execution_active = lambda _execution_id: True  # type: ignore[method-assign]
    state.predictor.finish_execution = finish_execution  # type: ignore[method-assign]
    with TestClient(create_app(state)) as client:
        registration = client.post(
            "/v2/executions",
            json={
                "execution_id": "call-exec",
                "tool_call_id": "call-exec",
                "run_id": "run-launcher-exec",
                "session_key_hash": None,
                "command_digest": "sha256:" + "d" * 64,
                "command": "pip install flask falcon starlette | tail",
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
                "launcher_pid": 100,
            },
        ).json()
        completion = {
            "schema_version": "clawtune.v1",
            "event_id": "evt-exec-yielded",
            "occurred_at": "2026-07-16T03:23:01Z",
            "plugin_version": "0.1.0",
            "run_id": "run-launcher-exec",
            "session_id": "session-launcher-exec",
            "session_key": None,
            "agent_id": None,
            "tool_call_id": "call-exec",
            "decision_id": None,
            "lease_id": None,
            "execution_id": "call-exec",
            "tool_name": "exec",
            "duration_ms": 100,
            "succeeded": True,
            "error_type": None,
            "error_digest": None,
            "result_size_bytes": None,
            "raw_result": {"details": {"status": "running"}},
            "resource_scope": {
                "kind": "cgroup-v2",
                "execution_id": "call-exec",
                "cgroup_path": str(sandbox_cgroup),
            },
        }

        assert client.post("/v1/events/tool-completed", json=completion).json() == {
            "stored": True
        }
        assert finish_calls == []

        assert client.post(
            "/v2/executions/call-exec/exited",
            json={
                "update_token": claim["update_token"],
                "exit_code": 0,
                "signal": None,
            },
        ).json() == {"stored": True}
        assert finalized.wait(timeout=1.0)

    assert finish_calls == [
        {
            "execution_id": "call-exec",
            "exit_code": 0,
            "signal": None,
        }
    ]


def test_execution_started_host_cgroup_gate_creates_exact_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sandbox_cgroup = tmp_path / "sandbox-cgroup"
    _write_cgroup_fixture(sandbox_cgroup)
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            sandbox_cgroup_path=str(sandbox_cgroup),
            sandbox_container_id="b" * 64,
            execution_cgroup_root=str(tmp_path / "exact-root"),
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    begin_calls: list[dict[str, object]] = []

    def begin_execution(**kwargs):
        begin_calls.append(kwargs)
        return True

    state.predictor.begin_execution = begin_execution  # type: ignore[method-assign]
    monkeypatch.setattr(app_module, "_resolve_host_pid", lambda *_args, **_kwargs: 4242)
    monkeypatch.setattr(app_module, "_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(app_module, "_pid_namespace_inode", lambda _pid: 123)

    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-host-gate",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi",
            "workdir": "/workspace",
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
            "backend": "managed-wrapper",
        },
    ).json()
    claim = client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "call-exec",
            "token": registration["one_time_token"],
            "launcher_pid": 100,
        },
    ).json()
    assert begin_calls == []
    started = client.post(
        "/v2/executions/call-exec/started",
        json={
            "update_token": claim["update_token"],
            "launcher_pid": 100,
            "child_pid": 7,
            "process_starttime_ticks": 99,
            "cgroup_path": None,
            "pid_namespace_inode": 123,
            "container_id": None,
            "host_cgroup_gate": True,
        },
    )

    exact = tmp_path / "exact-root" / "call-exec"
    assert started.status_code == 200
    assert started.json() == {"stored": True, "cgroup_path": str(exact)}
    assert (exact / "cgroup.procs").read_text(encoding="utf-8").strip() == "4242"
    scope = client.get("/v2/executions/call-exec/scope").json()["execution_scope"]
    assert scope["cgroup_path"] == str(exact)
    assert scope["pid"] == 4242
    assert scope["attribution_source"] == "exclusive-execution-cgroup"
    assert begin_calls[-1]["cgroup_path"] == str(exact)


def test_execution_started_derives_host_cgroup_when_launcher_gate_off(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """host_cgroup_gate=False (the launcher created its own cgroup in the
    container) must still derive the launcher's ACTUAL host cgroup from
    /proc/<host_pid>/cgroup instead of jumping straight to per-PID psutil.
    The launcher-supplied container-namespace path is not host-valid and must
    not block the host derivation."""
    host_cgroup = tmp_path / "host-cgroup"
    _write_cgroup_fixture(host_cgroup)
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            sandbox_cgroup_path=str(host_cgroup),
            sandbox_container_id="b" * 64,
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    monkeypatch.setattr(app_module, "_resolve_host_pid", lambda *_args, **_kwargs: 4242)
    monkeypatch.setattr(
        app_module,
        "_host_cgroup_path_for_pid",
        lambda _pid: str(host_cgroup.resolve()),
    )
    monkeypatch.setattr(app_module, "_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(app_module, "_pid_namespace_inode", lambda _pid: 123)

    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-host-cgroup-derive",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi",
            "workdir": "/workspace",
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
            "backend": "managed-wrapper",
        },
    ).json()
    claim = client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "call-exec",
            "token": registration["one_time_token"],
            "launcher_pid": 100,
        },
    ).json()
    started = client.post(
        "/v2/executions/call-exec/started",
        json={
            "update_token": claim["update_token"],
            "launcher_pid": 100,
            "child_pid": 7,
            "process_starttime_ticks": 99,
            "cgroup_path": "/clawtune-executions/call-exec",
            "pid_namespace_inode": 123,
            "container_id": "b" * 64,
            "host_cgroup_gate": False,
        },
    )

    assert started.status_code == 200
    scope = client.get("/v2/executions/call-exec/scope").json()["execution_scope"]
    assert scope["kind"] == "cgroup-v2"
    assert scope["cgroup_path"] == str(host_cgroup.resolve())
    assert scope["attribution_source"] == "trusted-execution-root-pid"
    assert scope["root_pid"] == 4242


def test_execution_started_falls_back_to_per_pid_when_host_cgroup_unresolvable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """When /proc cannot yield a host cgroup for the launcher pid, the scope
    degrades to per-PID process-tree attribution (still attributed, not lost)."""
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            sandbox_container_id="b" * 64,
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    monkeypatch.setattr(app_module, "_resolve_host_pid", lambda *_args, **_kwargs: 4242)
    monkeypatch.setattr(app_module, "_host_cgroup_path_for_pid", lambda _pid: None)
    monkeypatch.setattr(app_module, "_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(app_module, "_pid_namespace_inode", lambda _pid: 123)

    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-host-cgroup-fallback",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi",
            "workdir": "/workspace",
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
            "backend": "managed-wrapper",
        },
    ).json()
    claim = client.post(
        "/v2/executions/claim",
        json={
            "execution_id": "call-exec",
            "token": registration["one_time_token"],
            "launcher_pid": 100,
        },
    ).json()
    started = client.post(
        "/v2/executions/call-exec/started",
        json={
            "update_token": claim["update_token"],
            "launcher_pid": 100,
            "child_pid": 7,
            "process_starttime_ticks": 99,
            "cgroup_path": "/clawtune-executions/call-exec",
            "pid_namespace_inode": 123,
            "container_id": "b" * 64,
            "host_cgroup_gate": False,
        },
    )

    assert started.status_code == 200
    scope = client.get("/v2/executions/call-exec/scope").json()["execution_scope"]
    assert scope["kind"] == "pid"
    assert scope["cgroup_path"] is None
    assert scope["attribution_source"] == "trusted-execution-root-pid"
    assert scope["root_pid"] == 4242


def test_ebpf_execution_starts_when_sandbox_scope_arrives_after_started(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(app_module, "_resolve_host_pid", lambda *_args, **_kwargs: 4242)
    call_cgroup = tmp_path / "call-cgroup"
    call_cgroup.mkdir()
    monkeypatch.setattr(
        app_module,
        "_host_cgroup_path_for_pid",
        lambda _pid: str(call_cgroup.resolve()),
    )
    monkeypatch.setattr(
        app_module,
        "_canonical_cgroup_path",
        lambda path, **_kwargs: Path(path).resolve(),
    )
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    begin_calls: list[dict[str, object]] = []

    def begin_execution(**kwargs):
        begin_calls.append(kwargs)
        return True

    state.predictor.begin_execution = begin_execution  # type: ignore[method-assign]

    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-launcher-exec",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi && true",
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
            "cgroup_path": str(call_cgroup),
            "pid_namespace_inode": 456,
            "container_id": None,
        },
    )
    assert begin_calls == [
        {
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "command": "echo hi && true",
            "container_id": None,
            "repo": "openclaw",
            "cgroup_path": str(call_cgroup),
            "trusted_root_pid": 4242,
        }
    ]
    begin_calls.clear()

    client.post(
        "/v1/runtime/sandbox-scope",
        json={
            "kind": "cgroup-v2",
            "execution_id": None,
            "pid": os.getpid(),
            "root_pid": os.getpid(),
            "process_start_time": None,
            "root_starttime_ticks": None,
            "cgroup_path": str(tmp_path / "sandbox-cgroup"),
            "pid_namespace_inode": None,
            "container_id": "b" * 64,
            "include_children": True,
            "source": "openclaw-sandbox",
            "attribution_source": "shared-sandbox-container",
        },
    )

    assert begin_calls == [
        {
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "command": "echo hi && true",
            "container_id": "b" * 64,
            "repo": "openclaw",
            "trusted_root_pid": 4242,
        }
    ]


def test_required_ebpf_defers_claim_without_container_id(tmp_path: Path) -> None:
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    client = TestClient(create_app(state))
    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-launcher-exec",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi && true",
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
    )

    assert claim.status_code == 200


def test_required_ebpf_rejects_unavailable_collector_during_started(
    tmp_path: Path,
) -> None:
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    client = TestClient(create_app(state))
    state.predictor.begin_execution = lambda **kwargs: False  # type: ignore[method-assign]
    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-launcher-exec",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi",
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

    started = client.post(
        "/v2/executions/call-exec/started",
        json={
            "update_token": claim["update_token"],
            "launcher_pid": os.getpid(),
            "child_pid": os.getpid(),
            "process_starttime_ticks": 123,
            "cgroup_path": str(tmp_path / "call-cgroup"),
            "pid_namespace_inode": 456,
            "container_id": "b" * 64,
        },
    )

    assert started.status_code == 503
    detail = started.json()["detail"]
    assert detail["code"] == "tool_resource_ebpf_start_failed"
    assert detail["reason"]


def test_required_ebpf_starts_during_claim_with_sandbox_container_id(tmp_path: Path) -> None:
    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            sandbox_container_id="b" * 64,
        )
    )
    client = TestClient(create_app(state))
    begin_calls: list[dict[str, object]] = []

    def begin_execution(**kwargs):
        begin_calls.append(kwargs)
        return True

    state.predictor.begin_execution = begin_execution  # type: ignore[method-assign]
    registration = client.post(
        "/v2/executions",
        json={
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "run_id": "run-launcher-exec",
            "session_key_hash": None,
            "command_digest": "sha256:" + "c" * 64,
            "command": "echo hi && true",
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
    )

    assert claim.status_code == 200
    assert begin_calls == [
        {
            "execution_id": "call-exec",
            "tool_call_id": "call-exec",
            "command": "echo hi && true",
            "container_id": "b" * 64,
            "repo": "openclaw",
        }
    ]


def test_exec_completion_uses_registered_launcher_scope(tmp_path: Path) -> None:
    cgroup = tmp_path / "launcher-cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 100000\n", encoding="utf-8")
    (cgroup / "memory.current").write_text("4096\n", encoding="utf-8")
    (cgroup / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (cgroup / "cgroup.procs").write_text("", encoding="utf-8")
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            tool_resource_ebpf_required=False,
        )
    )
    client = TestClient(create_app(state))
    request: dict[str, object] = {
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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
    assert tool_end["execution"]["source"] == "clawtune-launch"
    assert tool_end["execution"]["cgroup_path"] == str(cgroup)
    assert tool_end["resources"]["attribution_source"] == "clawtune-launch"
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
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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
    # LLM spans must satisfy the same monotonic invariant as tool spans
    # (mono_end - mono_start == duration_ns). Regression for the collapsed
    # monotonic-stamp bug in _record_model_v6.
    assert model_ends[0]["duration_ns"] == str(2000 * 1_000_000)
    assert (
        int(model_ends[0]["monotonic_time_ns"]) - int(model_starts[0]["monotonic_time_ns"])
        == int(model_ends[0]["duration_ns"])
    )


def test_llm_span_monotonic_invariant_holds_when_duration_absent(tmp_path: Path) -> None:
    client, trace_dir = _trace_client(tmp_path)
    started = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-model-zero-start",
        "occurred_at": "2026-07-16T03:23:03Z",
        "plugin_version": "0.1.0",
        "run_id": "run-zero",
        "session_id": "session-zero",
        "session_key": None,
        "agent_id": "agent-zero",
        "event_type": "model_call_started",
        "call_id": "llm-zero",
        "provider": "test-provider",
        "model": "test-model",
        "duration_ms": None,
        "outcome": None,
        "context_token_budget": 8192,
        "raw_input": [{"role": "user", "content": "hi"}],
        "raw_output": None,
        "raw_event": {"messages": [{"role": "user", "content": "hi"}]},
    }
    ended = started | {
        "event_id": "evt-model-zero-end",
        "occurred_at": "2026-07-16T03:23:04Z",
        "event_type": "model_call_ended",
        "duration_ms": None,  # no reported duration
        "outcome": "success",
        "raw_input": None,
        "raw_output": "ok",
        "raw_event": {"content": "ok"},
    }
    assert client.post("/v1/events/model", json=started).json() == {"stored": True}
    assert client.post("/v1/events/model", json=ended).json() == {"stored": True}

    records = _read_trace_records(trace_dir)
    llm_starts = [r for r in records if r.get("record_type") == "span_start" and r.get("kind") == "llm"]
    llm_ends = [r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "llm"]
    assert len(llm_starts) == 1
    assert len(llm_ends) == 1
    # No reported duration -> duration_ns is 0 and the monotonic span is
    # zero-width; the invariant mono_end - mono_start == duration_ns holds.
    assert llm_ends[0]["duration_ns"] == "0"
    assert (
        int(llm_ends[0]["monotonic_time_ns"]) - int(llm_starts[0]["monotonic_time_ns"])
        == int(llm_ends[0]["duration_ns"])
    )
    assert llm_ends[0]["resources"]["action_duration_ns"] == "0"


def test_trace_marks_raw_exec_exit_code_failure(tmp_path: Path) -> None:
    client, trace_dir = _trace_client(tmp_path)
    request: dict[str, object] = {
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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
        "schema_version": "clawtune.v1",
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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)
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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    assert client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    ).status_code == 200

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = {
        "schema_version": "clawtune.v1",
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


def _drive_proxy_and_model_events(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gateway_id: str | None,
    runtime_id: str | None,
) -> None:
    """Send a proxied chat completion plus model start/end events.

    Mirrors a live agent turn: OpenClaw POSTs to the sidecar LLM proxy (which
    records ``messages_in``/``content`` keyed by the runtime credential) and the
    plugin separately reports ``model_call_started`` / ``model_call_ended``.
    The sidecar must correlate the proxy capture with the model span.
    """

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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    headers = {}
    if runtime_id is not None:
        headers["x-clawtune-runtime-id"] = runtime_id
    assert client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
        headers=headers,
    ).status_code == 200

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = {
        "schema_version": "clawtune.v1",
        "event_id": "evt-model-start-proxy",
        "occurred_at": now,
        "plugin_version": "0.1.0",
        "run_id": "run-proxy",
        "session_id": "session-proxy",
        "session_key": "agent:main:main",
        "agent_id": None,
        "gateway_id": gateway_id,
        "runtime_id": runtime_id,
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


def test_model_proxy_capture_correlates_when_events_carry_gateway_id(
    tmp_path: Path, monkeypatch
) -> None:
    """swe-rebench scenario: model events carry gateway_id="swe-rebench" and a
    runtime id, while proxy captures only carry the runtime credential. The
    proxy messages must still be attached to the LLM span."""
    client, trace_dir = _trace_proxy_client(tmp_path)
    _drive_proxy_and_model_events(
        client,
        monkeypatch,
        gateway_id="swe-rebench",
        runtime_id="clawtune-srb-996de1b4ee38",
    )

    records = _read_trace_records(trace_dir)
    llm_starts = [
        r for r in records if r.get("record_type") == "span_start" and r.get("kind") == "llm"
    ]
    assert len(llm_starts) == 1
    assert llm_starts[0]["gateway_id"] == "swe-rebench"
    assert llm_starts[0]["runtime_id"] == "clawtune-srb-996de1b4ee38"
    assert llm_starts[0]["input"]["messages"] == [{"role": "user", "content": "hello"}]
    llm_ends = [
        r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "llm"
    ]
    assert llm_ends[0]["output"]["content"]["content"] == "world"
    assert llm_ends[0]["output"]["content"]["tool_calls"][0]["id"] == "call-proxy-tool"


def test_model_proxy_capture_correlates_without_gateway_id(tmp_path: Path, monkeypatch) -> None:
    """Plain OpenClaw / legacy scenario: no gateway_id anywhere. Correlation
    still works on runtime_id + model + time alone."""
    client, trace_dir = _trace_proxy_client(tmp_path)
    _drive_proxy_and_model_events(client, monkeypatch, gateway_id=None, runtime_id=None)

    records = _read_trace_records(trace_dir)
    llm_starts = [
        r for r in records if r.get("record_type") == "span_start" and r.get("kind") == "llm"
    ]
    assert len(llm_starts) == 1
    assert llm_starts[0]["gateway_id"] is None
    assert llm_starts[0]["input"]["messages"] == [{"role": "user", "content": "hello"}]
    llm_ends = [
        r for r in records if r.get("record_type") == "span_end" and r.get("kind") == "llm"
    ]
    assert llm_ends[0]["output"]["content"]["content"] == "world"


def test_proxy_capture_rejected_when_explicit_gateway_differs(tmp_path: Path) -> None:
    """Isolation intent is preserved: when a proxy capture DOES carry an explicit
    gateway_id, a model event from a different gateway must not consume it."""
    from clawtune_sidecar.contracts.models import ModelEvent
    from clawtune_sidecar.trace import AgentTestBenchTraceWriter

    writer = AgentTestBenchTraceWriter(tmp_path / "traces")
    ts = time.time()
    event_kwargs = dict(
        schema_version="clawtune.v1",
        event_id="evt-1",
        occurred_at=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
        plugin_version="0.1.0",
        run_id="run-1",
        session_id="session-1",
        session_key="agent:main:main",
        agent_id=None,
        event_type="model_call_ended",
        call_id="c1",
        provider="vllm",
        model="test-model",
        duration_ms=1000,
        outcome="completed",
        context_token_budget=8192,
        raw_input=None,
        raw_output=None,
        raw_event=None,
        runtime_id="runtime-a",
        repo="owner/repo",
    )

    def _record_with_gateway(gateway_id: str) -> None:
        writer.record_llm_proxy_call(
            runtime_id="runtime-a",
            action_id=f"llm-proxy-{gateway_id}",
            provider="llm-proxy",
            model="test-model",
            messages_in=[{"role": "user", "content": "hello"}],
            content="world",
            raw_request=None,
            raw_response=None,
            ts_start=ts - 1.0,
            ts_end=ts,
            status_code=200,
            stream=False,
            error=None,
        )
        # The proxy does not emit a gateway_id today, but if a future capture
        # does, the strict isolation rule must still reject foreign gateways.
        writer._recent_proxy_calls[-1]["gateway_id"] = gateway_id

    _record_with_gateway("gateway-a")
    different = ModelEvent(**{**event_kwargs, "gateway_id": "gateway-b"})
    assert writer._pop_recent_proxy_call(different) is None
    # The rejected capture was not consumed; drop it so the next scenario starts
    # with a single unambiguous candidate.
    writer._recent_proxy_calls.clear()

    _record_with_gateway("gateway-a")
    same = ModelEvent(**{**event_kwargs, "gateway_id": "gateway-a"})
    matched = writer._pop_recent_proxy_call(same)
    assert matched is not None
    assert matched["data"]["messages_in"] == [{"role": "user", "content": "hello"}]


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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

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


def test_llm_proxy_parses_crlf_sse_before_end_of_stream() -> None:
    first = b'data: {"choices":[{"delta":{"content":"hello"}}]}\r\n\r\n'
    second = b"data: [DONE]\r\n\r\n"

    events, remainder = _parse_sse_buffer(first + second[:8])

    assert len(events) == 1
    assert events[0]["choices"][0]["delta"]["content"] == "hello"
    assert remainder == second[:8]


def test_llm_proxy_surfaces_empty_stream_and_writes_safe_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, trace_dir = _trace_proxy_client(tmp_path)

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert "upstream_empty_response" in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    debug_files = list(trace_dir.glob("llm_proxy_debug_*.json"))
    assert len(debug_files) == 1
    diagnostic = json.loads(debug_files[0].read_text(encoding="utf-8"))
    assert diagnostic["automatic_empty_diagnostic"] is True
    assert diagnostic["raw_preview_bytes"] > 0
    assert "raw_preview_sha256" in diagnostic
    assert "raw_preview" not in diagnostic


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

    monkeypatch.setattr("clawtune_sidecar.llm_proxy.httpx.AsyncClient", FakeAsyncClient)

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
        "source": "clawtune-launch",
        "attribution_source": "clawtune-launch",
    }

    exited = client.post(
        "/v2/executions/exec-1/exited",
        json={"update_token": spec["update_token"], "exit_code": 0, "signal": None},
    )
    assert exited.status_code == 200
    assert exited.json() == {"stored": True}
