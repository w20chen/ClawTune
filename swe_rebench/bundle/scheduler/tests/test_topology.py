from __future__ import annotations

from pathlib import Path

from agent_scheduler.api import dependencies
from agent_scheduler.config import SchedulerConfig
from agent_scheduler.topology import linux


def _linux_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    online: str,
    affinity: set[int],
    cpuset: str,
    cpu_max: str = "max 100000",
    parent_cpu_max: str = "max 100000",
    host_cpu_count: int | None = None,
) -> tuple[Path, Path]:
    cpu_root = tmp_path / "sys" / "cpu"
    node_root = tmp_path / "sys" / "node"
    cgroup_root = tmp_path / "cgroup"
    current = cgroup_root / "batch" / "runtime"
    cpu_root.mkdir(parents=True)
    node_root.mkdir(parents=True)
    current.mkdir(parents=True)
    (cpu_root / "online").write_text(online, encoding="utf-8")
    (current / "cpuset.cpus.effective").write_text(cpuset, encoding="utf-8")
    (current / "cpu.max").write_text(cpu_max, encoding="utf-8")
    (cgroup_root / "cpu.max").write_text(parent_cpu_max, encoding="utf-8")
    proc_self_cgroup = tmp_path / "proc-self-cgroup"
    proc_self_cgroup.write_text("0::/batch/runtime\n", encoding="utf-8")

    monkeypatch.setattr(linux.os, "name", "posix")
    monkeypatch.setattr(
        linux.os,
        "cpu_count",
        lambda: host_cpu_count if host_cpu_count is not None else len(affinity),
    )
    monkeypatch.setattr(linux.os, "sched_getaffinity", lambda _pid: affinity, raising=False)
    monkeypatch.setattr(linux, "_CPU_SYSFS_ROOT", cpu_root)
    monkeypatch.setattr(linux, "_NODE_SYSFS_ROOT", node_root)
    monkeypatch.setattr(linux, "_CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(linux, "_PROC_SELF_CGROUP", proc_self_cgroup)
    return cpu_root, node_root


def _cache(
    cpu_root: Path,
    cpu: int,
    index: int,
    *,
    level: int,
    cache_type: str,
    shared: str,
) -> None:
    root = cpu_root / f"cpu{cpu}" / "cache" / f"index{index}"
    root.mkdir(parents=True)
    (root / "level").write_text(str(level), encoding="utf-8")
    (root / "type").write_text(cache_type, encoding="utf-8")
    (root / "shared_cpu_list").write_text(shared, encoding="utf-8")


def test_capacity_scales_to_320_cpus_without_machine_size_hardcode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _linux_fixture(
        tmp_path,
        monkeypatch,
        online="0-319",
        affinity=set(range(320)),
        cpuset="0-319",
        host_cpu_count=320,
    )

    topology = linux.read_topology()

    assert topology["effective_cpus"] == list(range(320))
    assert topology["cpu_capacity_cores"] == 320.0
    assert topology["reserved_cpu_cores"] == 16
    assert topology["tool_cpu_budget_cores"] == 304.0


def test_capacity_intersects_all_cpu_masks_and_tightest_ancestor_quota(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _linux_fixture(
        tmp_path,
        monkeypatch,
        online="0-15",
        affinity=set(range(2, 12)),
        cpuset="4-9",
        cpu_max="800000 100000",
        parent_cpu_max="400000 100000",
        host_cpu_count=16,
    )

    topology = linux.read_topology()

    assert topology["effective_cpus"] == list(range(4, 10))
    assert topology["cpu_quota_cores"] == 4.0
    assert topology["cpu_capacity_cores"] == 4.0
    assert topology["reserved_cpu_cores"] == 1
    assert topology["tool_cpu_budget_cores"] == 3.0


def test_capacity_keeps_one_cpu_available_and_honors_explicit_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _linux_fixture(
        tmp_path,
        monkeypatch,
        online="0",
        affinity={0},
        cpuset="0",
        host_cpu_count=1,
    )

    topology = linux.read_topology(
        reserve_ratio=1.0,
        reserve_cores=20,
        cpu_budget_cores=0.5,
    )

    assert topology["cpu_capacity_cores"] == 1.0
    assert topology["reserved_cpu_cores"] == 0
    assert topology["tool_cpu_budget_cores"] == 0.5


def test_arm_llc_uses_highest_data_or_unified_cache_not_fixed_l3(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cpu_root, node_root = _linux_fixture(
        tmp_path,
        monkeypatch,
        online="0-3",
        affinity={0, 1, 2, 3},
        cpuset="0-3",
        host_cpu_count=4,
    )
    for cpu in range(4):
        _cache(cpu_root, cpu, 0, level=1, cache_type="Data", shared=str(cpu))
        shared = "0-1" if cpu < 2 else "2-3"
        _cache(cpu_root, cpu, 1, level=2, cache_type="Unified", shared=shared)
        # A higher instruction-only cache is not an LLC for data placement.
        _cache(cpu_root, cpu, 2, level=3, cache_type="Instruction", shared="0-3")
    node0 = node_root / "node0"
    node1 = node_root / "node1"
    node0.mkdir()
    node1.mkdir()
    (node0 / "cpulist").write_text("0-1", encoding="utf-8")
    (node1 / "cpulist").write_text("2-3", encoding="utf-8")

    topology = linux.read_topology()

    assert topology["llc_clusters"] == [
        {
            "level": 2,
            "cache_type": "Unified",
            "shared_cpu_list": "0-1",
            "cpus": [0, 1],
        },
        {
            "level": 2,
            "cache_type": "Unified",
            "shared_cpu_list": "2-3",
            "cpus": [2, 3],
        },
    ]
    assert topology["numa_nodes"] == [
        {"node": 0, "cpulist": "0-1", "cpus": [0, 1]},
        {"node": 1, "cpulist": "2-3", "cpus": [2, 3]},
    ]


def test_build_state_applies_configured_capacity_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_read_topology(**options: object) -> dict[str, object]:
        captured.update(options)
        return {"available": True}

    monkeypatch.setattr(dependencies, "read_topology", fake_read_topology)

    state = dependencies.build_state(
        SchedulerConfig(
            trace_dir=tmp_path / "traces",
            tool_resource_artifact_dir=tmp_path / "artifacts",
            tool_resource_stage2_required=False,
            cpu_reserve_ratio=0.125,
            cpu_reserve_cores=3,
            cpu_budget_cores=12.5,
        )
    )

    assert state.topology == {
        "available": True,
        "max_active_tools": 1,
        "max_active_tools_source": "effective_cpu_budget",
        "tool_cpu_budget_mcpu": None,
    }
    assert captured == {
        "reserve_ratio": 0.125,
        "reserve_cores": 3,
        "cpu_budget_cores": 12.5,
    }
