"""Tests for the independent cgroup resource artifact and trace wiring."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_scheduler.contracts.models import ToolBeforeRequest, ToolCompletedEvent
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from agent_scheduler.telemetry.cgroup_resource import (
    build_cgroup_resource,
    write_cgroup_resource,
)
from agent_scheduler.trace import AgentTestBenchTraceWriter


def _sample(*, monitor_source: str = "cgroup-v2") -> ToolRuntimeSample:
    ts = time.time()
    return ToolRuntimeSample(
        event_id="evt-completed",
        tool_call_id="call-exec-1",
        tool_name="exec",
        operation="exec",
        started_at=ts - 0.6,
        ended_at=ts,
        duration_ms=615,
        monitor_duration_ms=611,
        monitor_start_wall_s=ts - 0.6,
        monitor_end_wall_s=ts,
        monitor_start_monotonic_s=None,
        monitor_end_monotonic_s=None,
        cpu_time_delta_s=0.239,
        rss_bytes_before=7_000_000,
        rss_bytes_after=7_200_000,
        read_bytes_delta=4096,
        write_bytes_delta=8192,
        net_rx_bytes_delta=1234,
        net_tx_bytes_delta=5678,
        ctx_switches_delta=0,
        rss_bytes_peak=7_500_000,
        cpu_utilization_avg_cores=0.4,
        cpu_utilization_avg_pct=40.0,
        disk_read_bytes_per_s=0.0,
        disk_write_bytes_per_s=0.0,
        net_rx_bytes_per_s=0.0,
        net_tx_bytes_per_s=0.0,
        sampling_interval_ms=50,
        sampling_point_count=13,
        sampling_quality="ok",
        resource_timeline=[],
        resource_timeline_truncated=False,
        resource_class="latency_medium",
        target_pid=None,
        process_count_before=None,
        process_count_after=None,
        attribution_status="partially_attributed",
        monitor_source=monitor_source,
    )


def _completed_event() -> ToolCompletedEvent:
    ts = time.time()
    return ToolCompletedEvent(
        schema_version="scheduler.v1",
        event_id="evt-completed",
        occurred_at=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
        plugin_version="0.1.0",
        run_id="run-1",
        session_id="session-1",
        session_key="agent:main:main",
        agent_id=None,
        gateway_id="swe-rebench",
        runtime_id="claw-srb-x",
        repo="owner/repo",
        tool_call_id="call-exec-1",
        decision_id=None,
        lease_id=None,
        execution_id="exec-abc",
        tool_name="exec",
        duration_ms=615,
        succeeded=True,
        error_type=None,
        error_digest=None,
        result_size_bytes=10,
        raw_result=None,
        resource_scope=None,
    )


def _before_event() -> ToolBeforeRequest:
    ts = time.time()
    return ToolBeforeRequest(
        schema_version="scheduler.v1",
        event_id="evt-before",
        occurred_at=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
        plugin_version="0.1.0",
        run_id="run-1",
        session_id="session-1",
        session_key="agent:main:main",
        agent_id=None,
        gateway_id="swe-rebench",
        runtime_id="claw-srb-x",
        repo="owner/repo",
        tool_call_id="call-exec-1",
        tool_name="exec",
        tool_kind=None,
        tool_input_kind=None,
        derived_paths=[],
        params_digest="sha256:" + "0" * 64,
        param_features={
            "serialized_size_bytes": 10,
            "string_length": 5,
            "list_item_count": 1,
            "path_count": 1,
            "has_command_like_field": True,
        },
        raw_params={"command": "pytest -q"},
        raw_event=None,
        resource_scope=None,
    )


def test_write_cgroup_resource_writes_independent_artifact(tmp_path: Path) -> None:
    sample = _sample()
    rel = write_cgroup_resource(
        tmp_path,
        sample,
        execution_id="exec-abc",
        tool_call_id="call-exec-1",
        tool_name="exec",
        attribution_source="shared-sandbox-container",
    )
    assert rel == "tool-resource/cgroup-resource-exec-abc.json"
    path = tmp_path / rel
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == "cgroup_resource_v1"
    assert data["source"] == "cgroup-v2"
    assert data["execution_id"] == "exec-abc"
    # cpu / memory / disk / network must all be present
    assert data["cpu_time_s"] == 0.239
    assert data["memory_rss_peak_bytes"] == 7_500_000
    assert data["disk_read_bytes_delta"] == 4096
    assert data["disk_write_bytes_delta"] == 8192
    assert data["network_rx_bytes_delta"] == 1234
    assert data["network_tx_bytes_delta"] == 5678
    assert data["attribution_source"] == "shared-sandbox-container"
    assert "independent of Stage-2" in data["independence"]


def test_write_cgroup_resource_skips_non_cgroup_or_no_exec(tmp_path: Path) -> None:
    # non-cgroup/process-tree monitor source -> no artifact
    assert (
        write_cgroup_resource(
            tmp_path,
            _sample(monitor_source="process_tree"),
            execution_id="exec-abc",
            tool_call_id="c",
            tool_name="exec",
        )
        is None
    )
    # no execution id -> no artifact (in-process tools)
    assert (
        write_cgroup_resource(
            tmp_path,
            _sample(),
            execution_id=None,
            tool_call_id="c",
            tool_name="read",
        )
        is None
    )
    # None sample -> no artifact
    assert (
        write_cgroup_resource(tmp_path, None, execution_id="e", tool_call_id="c", tool_name="exec")
        is None
    )


def test_write_cgroup_resource_psutil_process_tree(tmp_path: Path) -> None:
    sample = _sample(monitor_source="psutil-process-tree")
    rel = write_cgroup_resource(
        tmp_path,
        sample,
        execution_id="exec-pid-1",
        tool_call_id="c",
        tool_name="exec",
        attribution_source="trusted-execution-root-pid",
    )
    assert rel == "tool-resource/cgroup-resource-exec-pid-1.json"
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["source"] == "process-tree"
    assert data["monitor_source"] == "psutil-process-tree"
    assert data["cpu_time_s"] == 0.239
    assert data["network_rx_bytes_delta"] == 1234


def test_verified_host_pid_scope_helpers() -> None:
    from agent_scheduler.api.app import (
        _is_verified_host_pid_scope,
        _verified_host_pid_scope,
    )
    from agent_scheduler.contracts.models import ResourceScope

    scope = _verified_host_pid_scope("exec-1", 4242, 123)
    assert scope.kind == "pid"
    assert scope.pid == 4242
    assert scope.root_pid == 4242
    assert scope.root_starttime_ticks == 123
    assert scope.include_children is True
    assert scope.attribution_source == "trusted-execution-root-pid"
    assert _is_verified_host_pid_scope(scope)

    sandbox = ResourceScope(
        kind="cgroup-v2",
        cgroup_path="/sys/fs/cgroup/x",
        include_children=True,
        source="openclaw-sandbox",
        attribution_source="shared-sandbox-container",
    )
    assert not _is_verified_host_pid_scope(sandbox)
    assert not _is_verified_host_pid_scope(None)


def test_trace_writer_emits_cgroup_artifact_path(tmp_path: Path) -> None:
    writer = AgentTestBenchTraceWriter(tmp_path)
    writer.record_tool_started(_before_event())
    writer.record_tool(_completed_event(), _sample())
    assert writer.flush()

    records: list[dict] = []
    for path in tmp_path.glob("*.jsonl"):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    ends = [r for r in records if r.get("record_type") == "span_end"]
    assert ends
    res = ends[0]["resources"]
    assert res["cgroup_artifact_path"] == "tool-resource/cgroup-resource-exec-abc.json"
    artifact = tmp_path / "tool-resource" / "cgroup-resource-exec-abc.json"
    assert artifact.exists()


def test_compact_clauses_maps_artifact_resource_keys(tmp_path: Path) -> None:
    # exercised through the public predictor helper (pure function)
    from agent_scheduler.predictors.tool_resource import _compact_clauses

    rows = [
        {
            "bin": "python",
            "argv": ["python", "x.py"],
            "status": {"state": "exited", "exit_code": 0},
            "availability": {
                "latency": "ok",
                "cpu": "ok",
                "memory": "ok",
                "disk_io": "ok",
                "status": "ok",
            },
            "ts_start": 1.0,
            "ts_end": 2.0,
            "latency_ms": 1000.0,
            # the Stage-2 artifact row keys:
            "cpu_ns_cumulative": 500_000_000,  # 0.5 s
            "peak_cpu_cores": 1.2,
            "sampled_peak_rss_mb": 64.0,
            "disk_io": {
                "read_bytes_total": 4096,
                "write_bytes_total": 8192,
                "availability": "ok",
            },
            "network_rx_bytes": None,
            "network_tx_bytes": None,
        },
        {
            # old-format row: seconds-form key only (unit-trap regression:
            # it must NOT be divided by 1e9 a second time).
            "bin": "sh",
            "argv": ["sh", "x.sh"],
            "status": {"state": "exited", "exit_code": 0},
            "availability": {
                "latency": "ok",
                "cpu": "ok",
                "memory": "ok",
                "disk_io": "ok",
                "status": "ok",
            },
            "ts_start": 1.0,
            "ts_end": 3.0,
            "latency_ms": 2000.0,
            "cumulative_cpu_s": 1.25,  # seconds form, no ns key present
            "peak_cpu_cores": 2.0,
            "sampled_peak_rss_mb": 128.0,
            "disk_io": {
                "read_bytes_total": 1024,
                "write_bytes_total": 2048,
                "availability": "ok",
            },
            "network_rx_bytes": None,
            "network_tx_bytes": None,
        },
    ]
    compact = _compact_clauses(rows)
    assert len(compact) == 2
    c = compact[0]
    assert c["cpu_ns_cumulative"] == 500_000_000
    assert c["cumulative_cpu_s"] == 0.5
    assert c["peak_cpu_cores"] == 1.2
    assert c["peak_memory_mb"] == 64.0
    assert c["disk_read_bytes"] == 4096
    assert c["disk_write_bytes"] == 8192
    assert c["network_rx_bytes"] is None
    assert c["network_tx_bytes"] is None
    c2 = compact[1]
    assert c2["cpu_ns_cumulative"] == 1_250_000_000  # seconds -> ns
    assert c2["cumulative_cpu_s"] == 1.25
    assert c2["peak_memory_mb"] == 128.0
