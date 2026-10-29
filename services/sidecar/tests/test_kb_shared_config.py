"""Lattice views use the configured combination order without changing estimators."""

import pytest

from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
from command_parser.service import observation_features
from tool_resource.runtime_kb import ClauseObservation, LatencyBuckets
from tool_time.lattice_kb import LatticeTimeKB


def observation(index=0):
    return ClauseObservation(
        repo="org/repo", bin="python", argv=("python", "-m", "pytest", "-q", "tests"),
        ts_start=float(index * 10), ts_end=float(index * 10 + 1), latency_ms=1000.0,
        cpu_ns_cumulative=2_000_000_000, cpu_peak_cores=3.0, sampled_peak_rss_mb=256.0,
        memory_total_peak_bytes=512.0, memory_extra_peak_bytes=256.0,
        memory_baseline_bytes=256.0, memory_eligible=True, memory_environment_id="test",
        memory_measurement="cgroup_v2_environment_union_v1")


@pytest.mark.parametrize("limit", [0, 1, 2, 6])
def test_all_views_and_background_updates_preserve_the_shared_limit(limit):
    row = observation()
    record = observation_features(row)
    lattice = LatticeTimeKB.fit([row], max_optional_features=limit)
    lattice.prepare()
    from itertools import combinations
    optional = sorted(record.features - record.core)
    expected = {record.core | frozenset(part) for size in range(min(limit, len(optional)) + 1)
                for part in combinations(optional, size)} | {record.features}
    assert set(lattice._nodes) == expected
    assert record.features in expected
    assert all(len(fs - record.core) <= limit or fs == record.features for fs in expected)
    assert lattice._resource_states
    assert all(set(state.nodes) == expected for state in lattice._resource_states.values())

    successor = lattice.fork_for_update()
    successor.observe_completed_clause(observation(1))
    successor.prepare()
    successor._advance(100)
    assert successor.max_optional_features == limit
    assert set(successor._nodes) == expected
    assert all(set(state.nodes) == expected for state in successor._resource_states.values())
    assert lattice.observation_count == 1
    restored = LatticeTimeKB.from_json_obj(successor.to_json_obj())
    restored.prepare()
    assert restored.max_optional_features == limit
    assert set(restored._nodes) == expected
    assert restored.to_json_obj() == successor.to_json_obj()


@pytest.mark.parametrize("value", [-1, True, 1.5, "2", None])
def test_invalid_feature_limits_are_rejected_everywhere(value):
    for create in (LatticeTimeKB,):
        with pytest.raises(ValueError, match="nonnegative integer"):
            create(max_optional_features=value)
    with pytest.raises(ValueError, match="nonnegative integer"):
        SidecarConfig(kb_max_optional_features=value)


def test_env_shared_limit_and_default(monkeypatch):
    assert LatticeTimeKB().max_optional_features == 6
    monkeypatch.setenv("CLAWTUNE_KB_MAX_OPTIONAL_FEATURES", "2")
    assert SidecarConfig.from_env().kb_max_optional_features == 2
    monkeypatch.setenv("CLAWTUNE_KB_MAX_OPTIONAL_FEATURES", "-1")
    with pytest.raises(ValueError):
        SidecarConfig.from_env()


def test_offline_cli_forwards_the_shared_limit(tmp_path, monkeypatch):
    from benchmarks import cli
    from offline import runner

    received = {}
    def run(dataset, output, **kwargs):
        received.update(kwargs)
        return {}

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(cli, "_offline_console_summary", lambda *args: {})
    assert cli.main(["offline", "--dataset", str(tmp_path), "--rss-unit", "MiB",
                     "--kb-max-optional-features", "2"]) == 0
    assert received["max_optional_features"] == 2


def test_runtime_reload_uses_one_limit_and_mismatch_does_not_overwrite(tmp_path):
    kwargs = dict(openclaw_trace_paths=(), ebpf_trace_paths=(), artifact_dir=tmp_path,
                  buckets=LatencyBuckets((100, 500, 2000, 10000)), max_optional_features=1)
    predictor = ToolResourcePredictor.from_traces(**kwargs)
    try:
        assert predictor.lattice_kb.max_optional_features == 1
        predictor.lattice_kb.observe_completed_clause(observation())
        predictor.lattice_kb._advance(100)
        predictor._persist_lattice_kb()
    finally:
        predictor.close()
    files = {p: p.read_bytes() for p in tmp_path.glob("*.json")}
    with pytest.raises(ValueError, match="Lattice snapshot.*configured=6"):
        ToolResourcePredictor.from_traces(**dict(kwargs, max_optional_features=6))
    assert all(p.read_bytes() == data for p, data in files.items())
    restored = ToolResourcePredictor.from_traces(**kwargs)
    try:
        assert restored.lattice_kb.max_optional_features == 1
    finally:
        restored.close()
