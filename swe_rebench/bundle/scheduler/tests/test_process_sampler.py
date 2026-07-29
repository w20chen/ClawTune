from __future__ import annotations

import os

from agent_scheduler.contracts.models import ResourceScope
from agent_scheduler.monitoring.process import ProcessResourceSampler


def test_process_sampler_reads_cgroup_v2_scope(tmp_path) -> None:
    (tmp_path / "cpu.stat").write_text("usage_usec 250000\n", encoding="utf-8")
    (tmp_path / "memory.current").write_text("4096\n", encoding="utf-8")
    (tmp_path / "io.stat").write_text("8:0 rbytes=10 wbytes=20 rios=1 wios=2\n", encoding="utf-8")
    (tmp_path / "cgroup.procs").write_text("", encoding="utf-8")

    snapshot = ProcessResourceSampler().snapshot(
        ResourceScope(
            kind="cgroup-v2",
            execution_id="exec-1",
            pid=123,
            root_pid=123,
            process_start_time=None,
            root_starttime_ticks=456,
            cgroup_path=str(tmp_path),
            pid_namespace_inode=None,
            container_id=None,
            include_children=True,
            source="claw-launch",
            attribution_source="claw-launch",
        )
    )

    assert snapshot.available is True
    assert snapshot.source == "cgroup-v2"
    assert snapshot.process_cpu_time_s == 0.25
    assert snapshot.rss_bytes == 4096
    assert snapshot.read_bytes == 10
    assert snapshot.write_bytes == 20
    assert snapshot.target_pid == 123


def test_process_sampler_reads_cgroup_v2_scope_without_pid(tmp_path) -> None:
    (tmp_path / "cpu.stat").write_text("usage_usec 500000\n", encoding="utf-8")
    (tmp_path / "memory.current").write_text("8192\n", encoding="utf-8")

    snapshot = ProcessResourceSampler().snapshot(
        ResourceScope(
            kind="cgroup-v2",
            execution_id=None,
            pid=None,
            root_pid=None,
            process_start_time=None,
            root_starttime_ticks=None,
            cgroup_path=str(tmp_path),
            pid_namespace_inode=None,
            container_id="sandbox-1",
            include_children=True,
            source="openclaw-sandbox",
            attribution_source="shared-sandbox-container",
        )
    )

    assert snapshot.available is True
    assert snapshot.source == "cgroup-v2"
    assert snapshot.process_cpu_time_s == 0.5
    assert snapshot.rss_bytes == 8192


def test_cgroup_v2_requires_core_cgroup_metrics(tmp_path, monkeypatch) -> None:
    sampler = ProcessResourceSampler()
    monkeypatch.setattr(sampler, "_read_proc_net_dev", lambda _pid: (100, 200))

    snapshot = sampler._snapshot_cgroup(
        1.0,
        1.0,
        ResourceScope(
            kind="cgroup-v2",
            execution_id="exec-1",
            pid=123,
            root_pid=123,
            process_start_time=None,
            root_starttime_ticks=None,
            cgroup_path=str(tmp_path),
            pid_namespace_inode=None,
            container_id=None,
            include_children=True,
            source="claw-launch",
            attribution_source="claw-launch",
        ),
    )

    assert snapshot.source == "cgroup-v2"
    assert snapshot.net_rx_bytes == 100
    assert snapshot.net_tx_bytes == 200
    assert snapshot.available is False


def test_cgroup_v2_scope_does_not_fall_back_to_process_tree(tmp_path) -> None:
    snapshot = ProcessResourceSampler().snapshot(
        ResourceScope(
            kind="cgroup-v2",
            execution_id="exec-1",
            pid=os.getpid(),
            root_pid=os.getpid(),
            process_start_time=None,
            root_starttime_ticks=None,
            cgroup_path=str(tmp_path),
            pid_namespace_inode=None,
            container_id=None,
            include_children=True,
            source="claw-launch",
            attribution_source="claw-launch",
        )
    )

    assert snapshot.available is False
    assert snapshot.source == "cgroup-v2"
    assert snapshot.target_pid == os.getpid()


def test_process_sampler_rejects_cgroup_root_scope() -> None:
    snapshot = ProcessResourceSampler().snapshot(
        ResourceScope(
            kind="cgroup-v2",
            execution_id="exec-1",
            pid=123,
            root_pid=123,
            process_start_time=None,
            root_starttime_ticks=None,
            cgroup_path="/sys/fs/cgroup",
            pid_namespace_inode=None,
            container_id=None,
            include_children=True,
            source="claw-launch",
            attribution_source="claw-launch",
        )
    )

    assert snapshot.available is False
    assert snapshot.source == "cgroup-root-unattributed"
    assert snapshot.target_pid is None
