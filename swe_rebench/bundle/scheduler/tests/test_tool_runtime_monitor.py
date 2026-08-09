from __future__ import annotations

import os

from agent_scheduler.contracts.models import ParamFeatures, ResourceScope, ToolBeforeRequest, ToolCompletedEvent
from agent_scheduler.monitoring.process import ProcessResourceSampler, ResourceSnapshot
from agent_scheduler.monitoring.tool_runtime import (
    RealtimeToolMonitor,
    _first_timeline_point_of_source,
    _net_window_delta,
    _relative_timeline,
    _snapshot_from_point,
)


class _FakeNetAccounting:
    available = True

    def __init__(self) -> None:
        self.reset_calls: list[list[int]] = []
        self.read_calls: list[list[int]] = []

    def reset(self, pids) -> None:
        self.reset_calls.append(list(pids))

    def rx_tx_for(self, pids):
        self.read_calls.append(list(pids))
        return 100, 200

    def add_namespace(self, _inode) -> None:
        pass

    def close(self) -> None:
        pass


class QueueSampler:
    def __init__(self, snapshots: list[ResourceSnapshot]) -> None:
        self.snapshots = snapshots
        self.net_modes: list[str] = []

    def snapshot(
        self,
        scope: ResourceScope | None = None,
        *,
        net_mode: str = "ignore",
    ) -> ResourceSnapshot:
        self.net_modes.append(net_mode)
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


def test_docker_exec_pid_binding_rebases_shared_cgroup_sample() -> None:
    sampler = QueueSampler(
        [
            _snapshot(
                captured_at=20.0,
                cpu_s=100.0,
                rss=1_000_000,
                available=True,
                source="cgroup-v2",
            ),
            _snapshot(
                captured_at=20.1,
                cpu_s=1.0,
                rss=4096,
                available=True,
                source="process-tree",
            ),
            _snapshot(
                captured_at=20.3,
                cpu_s=1.2,
                rss=8192,
                available=True,
                source="process-tree",
            ),
        ]
    )
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    monitor.begin(
        _request(
            ResourceScope(
                kind="cgroup-v2",
                pid=10,
                root_pid=10,
                cgroup_path="/sys/fs/cgroup/docker",
                source="openclaw-sandbox",
                attribution_source="shared-sandbox-container",
            )
        ),
        "unknown",
    )
    pid_scope = ResourceScope(
        kind="pid",
        pid=123,
        root_pid=123,
        source="docker-events",
        attribution_source="docker-exec-pid",
    )

    assert monitor.bind_scope("call-1", pid_scope) is True
    assert monitor.bind_scope("call-1", pid_scope) is True
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
            execution_id=None,
            tool_name="read",
            duration_ms=200,
            succeeded=True,
            error_type=None,
            error_digest=None,
            result_size_bytes=None,
            resource_scope=pid_scope,
        )
    )

    assert sample.monitor_source == "process-tree"
    assert sample.attribution_status == "pid"
    assert abs((sample.cpu_time_delta_s or 0) - 0.2) < 0.001
    assert sample.rss_bytes_before == 4096
    assert sample.rss_bytes_after == 8192


def test_trusted_exec_pid_binding_rebases_shared_cgroup_baseline() -> None:
    # Reproduces the launcher/exec trace bug: the tool begins on the shared
    # sandbox container cgroup (large cumulative usage_usec), then the verified
    # host PID scope is bound mid-flight.  Without a rebase the end (per-pid
    # psutil) minus start (container cgroup) is negative and clamps to 0.0.
    sampler = QueueSampler(
        [
            _snapshot(
                captured_at=30.0,
                cpu_s=100.0,
                rss=1_000_000,
                available=True,
                source="cgroup-v2",
            ),
            _snapshot(
                captured_at=30.1,
                cpu_s=1.0,
                rss=4096,
                available=True,
                source="psutil-process-tree",
            ),
            _snapshot(
                captured_at=30.3,
                cpu_s=1.2,
                rss=8192,
                available=True,
                source="psutil-process-tree",
            ),
        ]
    )
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    monitor.begin(
        _request(
            ResourceScope(
                kind="cgroup-v2",
                pid=10,
                root_pid=10,
                cgroup_path="/sys/fs/cgroup/system.slice/docker-x.scope",
                source="openclaw-sandbox",
                attribution_source="shared-sandbox-container",
            )
        ),
        "unknown",
    )
    pid_scope = ResourceScope(
        kind="pid",
        pid=123,
        root_pid=123,
        root_starttime_ticks=987.0,
        source="claw-sidecar-host-derived",
        attribution_source="trusted-execution-root-pid",
    )

    assert monitor.bind_scope("call-1", pid_scope) is True
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
            resource_scope=pid_scope,
        )
    )

    assert sample.monitor_source == "psutil-process-tree"
    assert sample.attribution_status == "pid"
    # Without the rebase this was max(0, 1.2 - 100.0) == 0.0.
    assert abs((sample.cpu_time_delta_s or 0) - 0.2) < 0.001
    assert sample.rss_bytes_before == 4096
    assert sample.rss_bytes_after == 8192
    assert sample.rss_bytes_peak == 8192


def test_complete_cross_source_guard_emits_none_not_garbage() -> None:
    # No bind_scope ran: the baseline stays on the container cgroup while the
    # completion resolves to a per-pid scope.  complete() must not subtract the
    # foreign cgroup baseline; it should emit None deltas instead of 0.0.
    sampler = QueueSampler(
        [
            _snapshot(
                captured_at=40.0,
                cpu_s=100.0,
                rss=1_000_000,
                available=True,
                source="cgroup-v2",
            ),
            _snapshot(
                captured_at=40.3,
                cpu_s=1.2,
                rss=8192,
                available=True,
                source="psutil-process-tree",
            ),
        ]
    )
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    monitor.begin(
        _request(
            ResourceScope(
                kind="cgroup-v2",
                pid=10,
                root_pid=10,
                cgroup_path="/sys/fs/cgroup/docker",
                source="openclaw-sandbox",
                attribution_source="shared-sandbox-container",
            )
        ),
        "unknown",
    )
    pid_scope = ResourceScope(
        kind="pid",
        pid=123,
        root_pid=123,
        source="claw-sidecar-host-derived",
        attribution_source="trusted-execution-root-pid",
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
            resource_scope=pid_scope,
        )
    )

    assert sample.monitor_source == "psutil-process-tree"
    assert sample.cpu_time_delta_s is None
    assert sample.rss_bytes_before is None
    assert sample.rss_bytes_after == 8192


def _snapshot_with_net(
    *,
    captured_at: float,
    cpu_s: float | None,
    net_rx: int | None,
    net_tx: int | None,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        captured_at=captured_at,
        monotonic_s=captured_at,
        process_cpu_time_s=cpu_s,
        rss_bytes=1_000_000,
        read_bytes=None,
        write_bytes=None,
        net_rx_bytes=net_rx,
        net_tx_bytes=net_tx,
        ctx_switches=None,
        target_pid=123,
        process_count=1,
        available=True,
        source="cgroup-v2",
    )


def test_complete_cgroup_net_falls_back_to_last_live_sample() -> None:
    # A cgroup-scoped tool's process usually exits before the completion
    # snapshot, so the final /proc/<pid>/net/dev read fails (end net is None)
    # even though the live window baseline had net values.  complete() must
    # fall back to the last live net sample so the window aggregate is a
    # number, not None.
    sampler = QueueSampler(
        [
            _snapshot_with_net(captured_at=40.0, cpu_s=100.0, net_rx=1_000, net_tx=500),
            _snapshot_with_net(captured_at=40.3, cpu_s=105.0, net_rx=None, net_tx=None),
        ]
    )
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    cgroup_scope = ResourceScope(
        kind="cgroup-v2",
        pid=10,
        root_pid=10,
        cgroup_path="/sys/fs/cgroup/system.slice/docker-x.scope",
        source="claw-sidecar-host-derived",
        attribution_source="trusted-execution-root-pid",
    )
    monitor.begin(_request(cgroup_scope), "unknown")
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
            duration_ms=300,
            succeeded=True,
            error_type=None,
            error_digest=None,
            result_size_bytes=None,
            resource_scope=cgroup_scope,
        )
    )

    assert sample.monitor_source == "cgroup-v2"
    # Without the fallback the end net is None and the delta would be None;
    # with the last-live-sample fallback it is a real window aggregate (0 here
    # because no poll ran, but crucially not None).
    assert sample.net_rx_bytes_delta == 0
    assert sample.net_tx_bytes_delta == 0


def test_net_window_delta_uses_last_live_timeline_sample() -> None:
    # The completion snapshot is post-process-exit (end net None) but the poll
    # loop captured live points; the window aggregate must use the LAST live
    # net value as the end baseline.
    start = _snapshot_with_net(captured_at=40.0, cpu_s=100.0, net_rx=1_000, net_tx=500)
    end = _snapshot_with_net(captured_at=40.5, cpu_s=105.0, net_rx=None, net_tx=None)
    timeline = [
        {"ts": 40.0, "net_rx_bytes": 1_000, "net_tx_bytes": 500},
        {"ts": 40.2, "net_rx_bytes": 4_000, "net_tx_bytes": 2_000},
        {"ts": 40.3, "net_rx_bytes": 5_000, "net_tx_bytes": 2_500},
        {"ts": 40.5, "net_rx_bytes": None, "net_tx_bytes": None},
    ]
    rx, tx = _net_window_delta(start, end, timeline)
    assert rx == 4_000  # 5000 - 1000
    assert tx == 2_000  # 2500 - 500


def test_net_window_delta_recovers_when_start_unattributed() -> None:
    # When the begin snapshot itself had no live net (late/unattributed start),
    # the first live timeline sample becomes the window base.
    start = _snapshot_with_net(captured_at=40.0, cpu_s=100.0, net_rx=None, net_tx=None)
    end = _snapshot_with_net(captured_at=40.5, cpu_s=105.0, net_rx=None, net_tx=None)
    timeline = [
        {"ts": 40.0, "net_rx_bytes": None, "net_tx_bytes": None},
        {"ts": 40.2, "net_rx_bytes": 3_000, "net_tx_bytes": 1_500},
        {"ts": 40.3, "net_rx_bytes": 5_000, "net_tx_bytes": 2_500},
    ]
    rx, tx = _net_window_delta(start, end, timeline)
    assert rx == 2_000  # 5000 - 3000
    assert tx == 1_000  # 2500 - 1500


def test_net_window_delta_all_none_when_no_live_net() -> None:
    start = _snapshot_with_net(captured_at=40.0, cpu_s=100.0, net_rx=None, net_tx=None)
    end = _snapshot_with_net(captured_at=40.5, cpu_s=105.0, net_rx=None, net_tx=None)
    rx, tx = _net_window_delta(start, end, [])
    assert rx is None
    assert tx is None


def test_relative_timeline_resets_base_on_source_change() -> None:
    points = [
        {
            "ts": 10.0, "cpu_time_s": 100.0, "rss_bytes": 1_000_000,
            "read_bytes": 0, "write_bytes": 0, "net_rx_bytes": 0,
            "net_tx_bytes": 0, "ctx_switches": 0, "process_count": 1,
            "available": True, "source": "cgroup-v2",
        },
        {
            "ts": 10.1, "cpu_time_s": 1.0, "rss_bytes": 4096,
            "read_bytes": 0, "write_bytes": 0, "net_rx_bytes": 0,
            "net_tx_bytes": 0, "ctx_switches": 0, "process_count": 1,
            "available": True, "source": "psutil-process-tree",
        },
        {
            "ts": 10.3, "cpu_time_s": 1.2, "rss_bytes": 8192,
            "read_bytes": 0, "write_bytes": 0, "net_rx_bytes": 0,
            "net_tx_bytes": 0, "ctx_switches": 0, "process_count": 1,
            "available": True, "source": "psutil-process-tree",
        },
    ]
    timeline = _relative_timeline(points)
    # First psutil point starts a fresh segment: no foreign cgroup baseline.
    assert timeline[1]["cpu_time_delta_s"] == 0.0
    assert timeline[1]["elapsed_ms"] == 0
    assert abs(timeline[2]["cpu_time_delta_s"] - 0.2) < 0.001
    assert timeline[2]["elapsed_ms"] == 200


def test_relative_timeline_rates_unavailable_at_source_boundary() -> None:
    points = [
        {
            "ts": 10.0, "cpu_time_s": 100.0, "rss_bytes": 1_000_000,
            "read_bytes": 1_000, "write_bytes": 2_000, "net_rx_bytes": 3_000,
            "net_tx_bytes": 4_000, "ctx_switches": 50, "process_count": 1,
            "available": True, "source": "cgroup-v2",
        },
        {
            "ts": 10.1, "cpu_time_s": 1.0, "rss_bytes": 4096,
            "read_bytes": 10, "write_bytes": 20, "net_rx_bytes": 30,
            "net_tx_bytes": 40, "ctx_switches": 5, "process_count": 1,
            "available": True, "source": "psutil-process-tree",
        },
        {
            "ts": 10.3, "cpu_time_s": 1.2, "rss_bytes": 8192,
            "read_bytes": 15, "write_bytes": 25, "net_rx_bytes": 35,
            "net_tx_bytes": 45, "ctx_switches": 7, "process_count": 1,
            "available": True, "source": "psutil-process-tree",
        },
    ]
    timeline = _relative_timeline(points)
    # The boundary point starts a fresh segment; its per-second rates must be
    # unavailable, not a cross-source subtraction like (10 - 1000) / 0.1.
    assert timeline[1]["read_bytes_per_s"] is None
    assert timeline[1]["write_bytes_per_s"] is None
    assert timeline[1]["net_rx_bytes_per_s"] is None
    assert timeline[1]["net_tx_bytes_per_s"] is None
    assert timeline[1]["read_bytes_delta"] == 0
    # Within the new segment, rates are computed from the previous same-source
    # point: (15 - 10) bytes over 0.2 s == 25 bytes/s.
    assert abs((timeline[2]["read_bytes_per_s"] or 0) - 25.0) < 0.001


def test_first_timeline_point_of_source_respects_before_cutoff() -> None:
    points = [
        {"ts": 10.0, "cpu_time_s": 100.0, "source": "cgroup-v2", "available": True},
        {"ts": 10.1, "cpu_time_s": 1.0, "source": "psutil-process-tree", "available": True},
        {"ts": 10.3, "cpu_time_s": 1.2, "source": "psutil-process-tree", "available": True},
    ]
    assert _first_timeline_point_of_source(points, "psutil-process-tree", before=10.3)["ts"] == 10.1
    assert _first_timeline_point_of_source(points, "psutil-process-tree", before=10.2)["ts"] == 10.1
    assert _first_timeline_point_of_source(points, "psutil-process-tree", before=10.1) is None
    # snapshot round-trip preserves the counter values
    rebuilt = _snapshot_from_point(points[2], target_pid=123)
    assert rebuilt.source == "psutil-process-tree"
    assert rebuilt.process_cpu_time_s == 1.2
    assert rebuilt.target_pid == 123


def test_sampler_snapshot_net_reset_read_ignore() -> None:
    sampler = ProcessResourceSampler()
    fake = _FakeNetAccounting()
    sampler._net_accounting = lambda scope: fake  # type: ignore[method-assign]
    scope = ResourceScope(
        kind="pid",
        pid=os.getpid(),
        root_pid=os.getpid(),
        include_children=False,
        source="claw-sidecar-host-derived",
        attribution_source="trusted-execution-root-pid",
    )

    start = sampler.snapshot(scope, net_mode="reset")
    assert start.net_rx_bytes == 0
    assert start.net_tx_bytes == 0
    assert fake.reset_calls

    end = sampler.snapshot(scope, net_mode="read")
    assert end.net_rx_bytes == 100
    assert end.net_tx_bytes == 200
    assert fake.read_calls

    ignored = sampler.snapshot(scope, net_mode="ignore")
    assert ignored.net_rx_bytes is None
    assert ignored.net_tx_bytes is None
    assert len(fake.read_calls) == 1  # ignore did not consume a read


def test_snapshot_cgroup_uses_per_process_net() -> None:
    sampler = ProcessResourceSampler()
    fake = _FakeNetAccounting()
    sampler._net_accounting = lambda scope: fake  # type: ignore[method-assign]
    scope = ResourceScope(
        kind="cgroup-v2",
        pid=os.getpid(),
        root_pid=os.getpid(),
        cgroup_path=str(os.getcwd()),
        source="claw-sidecar-host-cgroup",
        attribution_source="exclusive-execution-cgroup",
    )
    # cpu/mem/io files are absent in this environment, but the per-process net
    # tracker must still be consulted for the cgroup's pid set.
    snap = sampler._snapshot_cgroup(1.0, 1.0, scope, net_mode="read")
    assert snap.net_rx_bytes == 100
    assert snap.net_tx_bytes == 200
    assert snap.source == "cgroup-v2"


def test_shared_container_scope_keeps_namespace_net() -> None:
    # The shared sandbox container cgroup's pid set overlaps every concurrent
    # tool, so the per-tgid BCC reset() would clobber other tools' windows.
    # Such scopes must keep the namespace-wide /proc/net/dev cumulative delta.
    sampler = ProcessResourceSampler()
    fake = _FakeNetAccounting()
    sampler._net_accounting = lambda scope: fake  # type: ignore[method-assign]
    sampler._read_proc_net_dev = lambda pid: (1000, 2000)  # type: ignore[method-assign]
    shared_scope = ResourceScope(
        kind="cgroup-v2",
        pid=os.getpid(),
        root_pid=os.getpid(),
        cgroup_path=str(os.getcwd()),
        source="openclaw-sandbox",
        attribution_source="shared-sandbox-container",
    )
    start = sampler.snapshot(shared_scope, net_mode="reset")
    end = sampler.snapshot(shared_scope, net_mode="read")
    # BCC tracker must not be consulted for the shared scope.
    assert not fake.reset_calls
    assert not fake.read_calls
    # Namespace cumulative values flow through so complete()'s delta works.
    assert start.net_rx_bytes == 1000
    assert start.net_tx_bytes == 2000
    assert end.net_rx_bytes == 1000
    assert end.net_tx_bytes == 2000
    # The per-execution cgroup is per-tool disjoint and still uses BCC.
    exec_scope = ResourceScope(
        kind="cgroup-v2",
        pid=os.getpid(),
        root_pid=os.getpid(),
        cgroup_path=str(os.getcwd()),
        source="claw-sidecar-host-cgroup",
        attribution_source="exclusive-execution-cgroup",
    )
    snap = sampler.snapshot(exec_scope, net_mode="read")
    assert snap.net_rx_bytes == 100
    assert snap.net_tx_bytes == 200
    assert fake.read_calls


def test_process_net_accounting_api_is_safe() -> None:
    from agent_scheduler.monitoring.net_accounting import ProcessNetAccounting

    acc = ProcessNetAccounting([1_234_567])
    try:
        acc.reset([1, 2, 3])
        acc.add_namespace(99_999)
        rx, tx = acc.rx_tx_for([1, 2, 3])
        assert isinstance(rx, int)
        assert isinstance(tx, int)
        assert rx >= 0 and tx >= 0
    finally:
        acc.close()


def test_monitor_passes_net_modes() -> None:
    sampler = QueueSampler(
        [
            _snapshot(
                captured_at=10.0, cpu_s=None, rss=None, available=False,
                source="unattributed",
            ),
            _snapshot(
                captured_at=10.1, cpu_s=1.0, rss=4096, available=True,
                source="psutil-process-tree",
            ),
            _snapshot(
                captured_at=10.3, cpu_s=1.2, rss=8192, available=True,
                source="psutil-process-tree",
            ),
        ]
    )
    monitor = RealtimeToolMonitor(sampler=sampler, poll_interval_s=60)
    monitor.begin(_request(), "unknown")
    pid_scope = ResourceScope(
        kind="pid",
        pid=123,
        root_pid=123,
        source="claw-sidecar-host-derived",
        attribution_source="trusted-execution-root-pid",
    )
    monitor.bind_scope("call-1", pid_scope)
    monitor.complete(
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
            resource_scope=pid_scope,
        )
    )
    # begin resets the baseline, bind rebases (also resets), complete reads.
    assert sampler.net_modes == ["reset", "reset", "read"]
