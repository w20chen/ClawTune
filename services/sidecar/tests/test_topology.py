from __future__ import annotations

from pathlib import Path

from clawtune_sidecar.api import dependencies
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.topology import linux


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
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            tool_resource_artifact_dir=tmp_path / "artifacts",
            tool_resource_ebpf_required=False,
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


def test_read_proc_stat_ticks_parses_per_cpu_lines(tmp_path: Path, monkeypatch) -> None:
    proc_stat = tmp_path / "proc-stat"
    proc_stat.write_text(
        "cpu  100 0 200 300 0 0 0 0 0 0\n"
        "cpu0 10 0 20 30 0 0 0 0 0 0\n"
        "cpu1 15 0 25 40 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(linux, "_PROC_STAT", proc_stat)

    ticks = linux._read_proc_stat_ticks()

    assert set(ticks) == {0, 1}
    assert ticks[0].busy() == 30
    assert ticks[0].total() == 60
    assert ticks[1].busy() == 40
    assert ticks[1].total() == 80


def test_read_proc_stat_ticks_tolerates_missing_proc_stat(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "no-proc-stat"
    monkeypatch.setattr(linux, "_PROC_STAT", missing)

    assert linux._read_proc_stat_ticks() == {}


def test_aggregate_node_delta_ignores_cpus_missing_from_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proc_stat = tmp_path / "proc-stat"
    proc_stat.write_text(
        "cpu0 0 0 0 100 0 0 0 0 0 0\ncpu1 0 0 0 100 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(linux, "_PROC_STAT", proc_stat)
    before = linux._read_proc_stat_ticks()
    proc_stat.write_text(
        "cpu0 50 0 0 150 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    after = linux._read_proc_stat_ticks()

    # cpu1 disappeared from the window: only cpu0 contributes.
    assert linux._aggregate_node_delta([0, 1], before, after) == (50, 100, 1)
    # A node whose CPUs are entirely absent yields no usable window.
    assert linux._aggregate_node_delta([7, 8], before, after) is None


def test_numa_cpu_usage_sampler_reports_per_node_total_utilization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    node_root = tmp_path / "sys" / "node"
    for node_id, cpulist in ((0, "0-1"), (1, "2-3")):
        node_dir = node_root / f"node{node_id}"
        node_dir.mkdir(parents=True)
        (node_dir / "cpulist").write_text(cpulist, encoding="utf-8")
    proc_stat = tmp_path / "proc-stat"

    def write_stat(cpu_ticks: dict[int, tuple[int, int]]) -> None:
        # cpu_ticks: cpu -> (user, idle); all other fields zero.
        lines = ["cpu 0 0 0 0 0 0 0 0 0 0"]
        for cpu, (user, idle) in sorted(cpu_ticks.items()):
            lines.append(f"cpu{cpu} {user} 0 0 {idle} 0 0 0 0 0 0")
        proc_stat.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr(linux, "_PROC_STAT", proc_stat)
    monkeypatch.setattr(linux, "_NODE_SYSFS_ROOT", node_root)
    monkeypatch.setattr(linux, "_user_hz", lambda: 100)
    clock = {"now": 1000.0}
    monkeypatch.setattr(linux.time, "monotonic", lambda: clock["now"])

    # Baseline (t=1000): every CPU idle with total=100 ticks.
    write_stat({0: (0, 100), 1: (0, 100), 2: (0, 100), 3: (0, 100)})
    sampler = linux.NumaCpuUsageSampler()
    assert sampler._nodes == [
        {"node": 0, "cpulist": "0-1", "cpus": [0, 1]},
        {"node": 1, "cpulist": "2-3", "cpus": [2, 3]},
    ]

    # 1s window: node0 CPUs run 50% busy (total grows to 200, 50 busy);
    # node1 CPUs stay idle (total grows to 200, 0 busy).
    clock["now"] = 1001.0
    write_stat({0: (50, 150), 1: (50, 150), 2: (0, 200), 3: (0, 200)})

    sample = sampler.sample()

    assert sample["available"] is True
    assert sample["sampled"] is True
    assert sample["node_count"] == 2
    assert sample["window_s"] == 1.0
    assert sample["user_hz"] == 100
    node0, node1 = sample["nodes"]
    assert node0["node"] == 0
    assert node0["cpulist"] == "0-1"
    assert node0["online_cpus"] == 2
    assert node0["cpu_utilization_pct"] == 50.0
    # 100 busy jiffies / 100 Hz / 1s window = 1.0 average busy cores.
    assert node0["busy_cores"] == 1.0
    assert node1["node"] == 1
    assert node1["cpu_utilization_pct"] == 0.0
    assert node1["busy_cores"] == 0.0


def test_numa_cpu_usage_sampler_reports_unavailable_without_proc_stat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    node_root = tmp_path / "sys" / "node"
    node_dir = node_root / "node0"
    node_dir.mkdir(parents=True)
    (node_dir / "cpulist").write_text("0-1", encoding="utf-8")
    missing = tmp_path / "no-proc-stat"
    monkeypatch.setattr(linux, "_PROC_STAT", missing)
    monkeypatch.setattr(linux, "_NODE_SYSFS_ROOT", node_root)
    monkeypatch.setattr(linux.time, "monotonic", lambda: 1000.0)

    sampler = linux.NumaCpuUsageSampler()
    sample = sampler.sample()

    assert sample["available"] is False
    assert sample["sampled"] is False
    assert sample["node_count"] == 1
    assert sample["nodes"][0]["node"] == 0
    assert sample["nodes"][0]["cpu_utilization_pct"] is None
    assert sample["nodes"][0]["busy_cores"] is None
