"""Benchmark compatibility for snapshots containing resource-only evidence."""

import json
from pathlib import Path

import pytest

from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB
from swe_rebench.host_openclaw import KnowledgeBaseSyncError, _validate_lattice_time_kb_snapshot


@pytest.mark.parametrize("source", [None, False, True, "true"])
def test_benchmark_handoff_validates_pipe_input_evidence(source):
    kb = LatticeTimeKB.fit([ClauseObservation(
        repo="org/repo", bin="python", argv=("python", "work.py"),
        ts_start=1, ts_end=2, latency_ms=100,
    )])
    snapshot = json.loads(json.dumps(kb.to_json_obj()))
    snapshot["observations"][0]["stdin_from_pipe"] = source
    if isinstance(source, str):
        with pytest.raises(KnowledgeBaseSyncError, match="stdin_from_pipe"):
            _validate_lattice_time_kb_snapshot(Path("kb.json"), snapshot)
    else:
        _validate_lattice_time_kb_snapshot(Path("kb.json"), snapshot)


@pytest.mark.parametrize("latency", [None, 0.0])
def test_benchmark_handoff_accepts_resource_only_observations(latency):
    kb = LatticeTimeKB.fit([ClauseObservation(
        repo="org/repo", bin="python", argv=("python", "work.py"),
        ts_start=1, ts_end=2, latency_ms=latency, sampled_peak_rss_mb=0,
    )])
    snapshot = json.loads(json.dumps(kb.to_json_obj()))
    _validate_lattice_time_kb_snapshot(Path("kb.json"), snapshot)


def test_benchmark_handoff_preserves_explicit_legacy_coverage_mode():
    kb = LatticeTimeKB.fit([ClauseObservation(
        repo="org/repo", bin="python", argv=("python", "work.py"),
        ts_start=1, ts_end=2, latency_ms=100,
    )], subset_coverage=False)
    snapshot = json.loads(json.dumps(kb.to_json_obj()))
    assert snapshot["node_generation"]["subset_coverage"] is False
    _validate_lattice_time_kb_snapshot(Path("kb.json"), snapshot)
    assert LatticeTimeKB.from_json_obj(snapshot)._subset_coverage is False
