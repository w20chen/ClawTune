from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import export_resource_lattice as exporter
from tool_time.lattice_kb import LatticeTimeKB


def artifact():
    return {"provenance": {"window_ns": 500_000_000}, "quota_cores": 8,
            "calls": [{"eligible_for_kb": True, "telemetry_quality": "ok", "clauses": [{
                "bin": "python", "argv": ["python", "work.py"], "latency_ms": 1000,
                "cpu_ns_cumulative": 2_000_000_000, "peak_cpu_cores": 3,
                "sampled_peak_rss_mb": 32, "eligible_for_kb": True, "telemetry_quality": "ok",
                "availability": {"latency": "ok", "cpu": "ok", "memory": "ok"},
                "provenance": {"boundary_coverage": {"has_exec": True, "has_exit": True}}
            }]}]}


def test_split_is_per_repo_per_task_all_attempts_and_reproducible(tmp_path, monkeypatch):
    dataset = tmp_path / "data"
    for repo, n in (("org__a", 5), ("org__b", 5), ("org__single", 1)):
        for task in range(n):
            for attempt in range(2):
                path = dataset / f"{repo}-{task}" / f"attempt_{attempt}" / "clause_telemetry.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(artifact()))
    monkeypatch.setattr(exporter, "_load_valid_artifact", lambda path: json.loads(path.read_text()))
    output = tmp_path / "out/kb.json"
    before = {p: p.read_bytes() for p in dataset.rglob("*.json")}
    manifest = exporter.export(dataset, output)
    assert len(manifest["train_tasks"]) == 9
    assert len(manifest["test_tasks"]) == 2
    assert not set(manifest["train_tasks"]) & set(manifest["test_tasks"])
    for repo in ("org__a", "org__b"):
        assert sum(t.startswith(repo + "-") for t in manifest["train_tasks"]) == 4
        assert sum(t.startswith(repo + "-") for t in manifest["test_tasks"]) == 1
    assert manifest["observation_count"] == 18
    assert {Path(s["path"]).parts[0] for s in manifest["sources"]} == set(manifest["train_tasks"])
    assert exporter.export(dataset, tmp_path / "out/second.json") == manifest
    assert before == {p: p.read_bytes() for p in dataset.rglob("*.json")}
    kb = LatticeTimeKB.from_json_obj(json.loads(output.read_text()))
    assert kb.observation_count == 18
    with pytest.raises(ValueError, match="read-only"):
        exporter.export(dataset, dataset / "kb.json")


def test_metric_quality_and_peak_window_filter_independently():
    data = artifact()
    data["provenance"]["window_ns"] = 10_000_000
    row = data["calls"][0]["clauses"][0]
    row["availability"]["memory"] = "unknown:insufficient_rss_samples"
    observations, errors = exporter.eligible_observations("org/repo", data)
    assert not errors
    assert observations[0].cpu_ns_cumulative == 2_000_000_000
    assert observations[0].peak_cpu_cores is None
    assert observations[0].sampled_peak_rss_mb is None
    row["provenance"]["exit_signal"] = 9
    assert exporter.eligible_observations("org/repo", data)[0] == []


def test_export_filters_only_downstream_dependency_consumers():
    data = artifact()
    base = data["calls"][0]["clauses"][0]
    data["calls"][0]["clauses"] = [
        base | {"bin": "grep", "argv": ["grep", "needle", "file"]},
        base | {
            "bin": "cat",
            "argv": ["cat", "file"],
            "in_pipe": True,
            "pipeline_position": 0,
        },
        base | {
            "bin": "grep",
            "argv": ["grep", "needle"],
            "in_pipe": True,
            "pipeline_position": 1,
        },
    ]
    observations, errors = exporter.eligible_observations("org/repo", data)
    assert not errors
    assert [(row.bin, row.pipeline_position) for row in observations] == [
        ("grep", -1),
        ("cat", 0),
    ]


@pytest.mark.parametrize("latency", [None, 0.0])
def test_benchmark_handoff_accepts_resource_only_v2_observations(latency):
    from tool_resource.runtime_kb import ClauseObservation
    from swe_rebench.host_openclaw import _validate_lattice_time_kb_snapshot
    kb = LatticeTimeKB.fit([ClauseObservation(
        repo="org/repo", bin="python", argv=("python", "work.py"),
        ts_start=1, ts_end=2, latency_ms=latency, sampled_peak_rss_mb=0,
    )])
    snapshot = json.loads(json.dumps(kb.to_json_obj()))
    _validate_lattice_time_kb_snapshot(Path("kb.json"), snapshot)
