from __future__ import annotations

from agent_scheduler.contracts.models import ParamFeatures, ResourceScope, ToolBeforeRequest, ToolCompletedEvent
from agent_scheduler.monitoring.process import ResourceSnapshot
from agent_scheduler.monitoring.tool_runtime import RealtimeToolMonitor


class QueueSampler:
    def __init__(self, snapshots: list[ResourceSnapshot]) -> None:
        self.snapshots = snapshots

    def snapshot(self, scope: ResourceScope | None = None) -> ResourceSnapshot:
        return self.snapshots.pop(0)


def _snapshot(
    *,
    captured_at: float,
    cpu_s: float | None,
    rss: int | None,
    available: bool,
    source: str,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        captured_at=captured_at,
        monotonic_s=captured_at,
        process_cpu_time_s=cpu_s,
        rss_bytes=rss,
        read_bytes=None,
        write_bytes=None,
        net_rx_bytes=None,
        net_tx_bytes=None,
        ctx_switches=None,
        target_pid=123,
        process_count=1,
        available=available,
        source=source,
    )


def _request(scope: ResourceScope | None = None) -> ToolBeforeRequest:
    return ToolBeforeRequest(
        schema_version="scheduler.v1",
        event_id="evt-start",
        occurred_at="2026-07-16T03:23:00Z",
        plugin_version="0.1.0",
        run_id="run-1",
        session_id="session-1",
        session_key=None,
        agent_id=None,
        tool_call_id="call-1",
        tool_name="exec",
        tool_kind="shell",
        tool_input_kind="json",
        operation_hint=None,
        derived_paths=[],
        params_digest="sha256:" + "a" * 64,
        param_features=ParamFeatures(
            serialized_size_bytes=10,
            string_length=2,
            list_item_count=0,
            path_count=0,
            has_command_like_field=True,
        ),
        raw_params={"command": "ls"},
        resource_scope=scope,
    )


def test_bind_scope_switches_unattributed_start_to_cgroup_baseline() -> None:
    sampler = QueueSampler(
        [
            _snapshot(
                captured_at=10.0,
                cpu_s=None,
                rss=None,
                available=False,
                source="unattributed",
            ),
            _snapshot(
                captured_at=10.1,
                cpu_s=1.0,
                rss=4096,
                available=True,
                source="cgroup-v2",
            ),
            _snapshot(
                captured_at=10.3,
                cpu_s=1.2,
                rss=8192,
                available=True,
                source="cgroup-v2",
            ),
        ]
    )
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    monitor.begin(_request(), "unknown")
    monitor.bind_scope(
        "call-1",
        ResourceScope(
            kind="cgroup-v2",
            execution_id="call-1",
            pid=123,
            root_pid=123,
            cgroup_path="/sys/fs/cgroup/claw/call-1",
            source="claw-launch",
            attribution_source="claw-launch",
        ),
    )

    sample = monitor.complete(
        ToolCompletedEvent(
            schema_version="scheduler.v1",
            event_id="evt-end",
            occurred_at="2026-07-16T03:23:01Z",
            plugin_version="0.1.0",
            run_id="run-1",
            session_id="session-1",
            session_key=None,
            agent_id=None,
            tool_call_id="call-1",
            decision_id=None,
            lease_id=None,
            execution_id="call-1",
            tool_name="exec",
            duration_ms=200,
            succeeded=True,
            error_type=None,
            error_digest=None,
            result_size_bytes=None,
            resource_scope=None,
        )
    )

    assert sample.monitor_source == "cgroup-v2"
    assert sample.attribution_status == "cgroup-v2"
    assert sample.cpu_time_delta_s is not None
    assert abs(sample.cpu_time_delta_s - 0.2) < 0.001
    assert sample.rss_bytes_before == 4096
    assert sample.rss_bytes_after == 8192
