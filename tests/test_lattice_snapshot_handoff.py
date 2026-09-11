"""Benchmark compatibility for snapshots containing resource-only evidence."""

import json
from pathlib import Path

import pytest

from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB
from swe_rebench.host_openclaw import _validate_lattice_time_kb_snapshot


@pytest.mark.parametrize("latency", [None, 0.0])
def test_benchmark_handoff_accepts_resource_only_observations(latency):
    kb = LatticeTimeKB.fit([ClauseObservation(
        repo="org/repo", bin="python", argv=("python", "work.py"),
        ts_start=1, ts_end=2, latency_ms=latency, sampled_peak_rss_mb=0,
    )])
    snapshot = json.loads(json.dumps(kb.to_json_obj()))
    _validate_lattice_time_kb_snapshot(Path("kb.json"), snapshot)
