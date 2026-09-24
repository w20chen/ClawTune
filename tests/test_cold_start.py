from __future__ import annotations

import json
from pathlib import Path

import pytest

from cold_start.export import FILENAMES, export
from cold_start.flat_loader import declared_sampling_interval_ms, read_task
from cold_start.manifest import build_manifest, task_identity, validate_manifest
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, CompletedCall, LatencyBuckets, RuntimeToolResourceKB, ToolCallQuery
from tool_time.lattice_kb import LatticeTimeKB


@pytest.mark.parametrize("name", ["pennylane__PennyLaneAI__pennylane-889.trace", "PennyLaneAI__pennylane-889.trace",
                                 "pennylane__PennyLaneAI__pennylane-889.trace.jsonl"])
def test_task_name_compatibility(name):
    assert task_identity(name) == ("PennyLaneAI/pennylane", "PennyLaneAI__pennylane-889")


def trace(task, command="python work.py"):
    return "\n".join(json.dumps(row) for row in [
        {"type": "trace_metadata", "trace_format_version": 5, "instance_id": task},
        {"type": "action", "action_type": "tool_exec", "instance_id": task, "action_id": "tool-1", "data": {
            "tool_name": "exec", "tool_call_id": "call-1", "tool_args": json.dumps({"command": command}),
            "duration_ms": 1000, "success": True,
            "resource_timeline": {"source": "cgroup_cpu_proc_net", "scope": "openclaw_exec_tool_interval", "summary": {"cpu_core_s": 2}},
            "resource_observation": {"tool_call_id": "call-1", "command": command, "eligible_for_kb": True,
                "telemetry_quality": "ok", "telemetry_status": "ok", "clauses": [{
                    "bin": "python", "argv": ["python", "work.py"], "eligible_for_kb": True, "telemetry_quality": "ok",
                    "availability": {"latency": "ok", "cpu": "ok", "cpu_time": "ok", "memory": "ok"}, "latency_ms": 1000,
                    "cpu_ns_cumulative": 2_000_000_000, "provenance": {"cpu_time_ns": 2_000_000_000}, "peak_cpu_cores": 3,
                    "cpu_window_profile": [{"span_s": .5, "cpu_cores": 3}], "sampled_peak_rss_mb": 6.213632,
                }]}}}]) + "\n"


def dataset(tmp_path):
    root = tmp_path / "dataset"
    root.mkdir()
    for repo, n in (("org__a", 5), ("org__b", 2), ("org__single", 1)):
        for i in range(n):
            task = f"{repo}-{i}"
            (root / f"prefix__{task}.trace.jsonl").write_text(trace(task), encoding="utf-8")
    # Same logical task in another corpus: never split its copies independently.
    (root / "alternate__org__a-0.trace.jsonl").write_text(trace("org__a-0"), encoding="utf-8")
    return root


def test_manifest_is_task_based_reproducible_and_detects_drift(tmp_path):
    root = dataset(tmp_path)
    manifest = build_manifest(root)
    assert manifest == build_manifest(root)
    assert len(manifest["train_tasks"]) == 6 and len(manifest["test_tasks"]) == 2
    assert len(manifest["tasks"]["org__a-0"]["files"]) == 2
    assert manifest["repositories"]["org/a"] == {"total": 5, "train": 4, "test": 1}
    assert "org__single-0" in manifest["train_tasks"]
    validate_manifest(manifest, root)
    (root / "prefix__org__a-0.trace.jsonl").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="dataset changed"):
        validate_manifest(manifest, root)


def test_flat_loader_units_and_unproven_call_resource_scope(tmp_path):
    path = tmp_path / "prefix__org__a-1.trace.jsonl"
    path.write_text(trace("org__a-1"), encoding="utf-8")
    loaded = read_task(path, repo="org/a", task_id="org__a-1", rss_unit="MB")
    assert loaded.clauses[0].sampled_peak_rss_mb * 1_000_000 == pytest.approx(6213632)
    assert loaded.clauses[0].cpu_peak_cores == 3
    assert loaded.calls[0].cpu_time_eligible is False
    assert loaded.call_actuals == [{
        "duration_ms": 1000.0,
        "cpu_time_seconds": 2.0,
        "cpu_avg_cores": 2.0,
        "cpu_peak_cores": 3.0,
    }]
    trusted = read_task(path, repo="org/a", task_id="org__a-1", rss_unit="MB", trust_call_cgroup=True)
    assert trusted.calls[0].cpu_time_seconds == 2 and trusted.calls[0].cpu_time_eligible


def test_flat_loader_records_declared_period_not_partial_sample_durations(tmp_path):
    records = [json.loads(line) for line in trace("org__a-1").splitlines()]
    records[1]["data"]["resource_timeline"]["sample_interval_s"] = .25
    records[1]["data"]["resource_timeline"]["samples"] = [
        {"dt_s": .2}, {"dt_s": .5}, {"dt_s": .8},
    ]
    path = tmp_path / "sampling.trace.jsonl"
    path.write_text("\n".join(map(json.dumps, records)) + "\n", encoding="utf-8")

    loaded = read_task(path, repo="org/a", task_id="org__a-1", rss_unit="MiB")

    assert loaded.sample_periods_ms == [250.0]


def test_v6_sampling_period_uses_millisecond_declaration():
    assert declared_sampling_interval_ms({"sampling_interval_ms": 125}) == 125


@pytest.mark.parametrize("metadata", [
    {"error_type": "TimeoutError"},
    {"error_type": "cancelled"},
    {"status": {"code": "aborted"}},
    {"censored": True},
    {"resource_timeline": {"censored": True}},
    {"resource_observation": {"unavailable_reason": "protocol_timeout"}},
])
def test_censored_flat_calls_cannot_train_any_backend(tmp_path, metadata):
    records = [json.loads(line) for line in trace("org__a-1").splitlines()]
    data = records[1]["data"]
    data.update(success=False, duration_ms=60000)
    for key, value in metadata.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    # Leave otherwise eligible clauses present to verify no partial resource
    # evidence is silently accepted after a call-level truncation signal.
    path = tmp_path / "timeout.trace.jsonl"
    path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
    loaded = read_task(path, repo="org/a", task_id="org__a-1", rss_unit="MB", trust_call_cgroup=True)
    assert len(loaded.calls) == 1 and loaded.calls[0].censored
    assert loaded.calls[0].cpu_time_seconds == 2  # retained as censored evidence
    assert loaded.counts["censored_calls"] == 1
    assert loaded.clauses == []
    runtime = RuntimeToolResourceKB()
    runtime.observe_completed_call(loaded.calls[0])
    assert runtime.predict_load_samples(ToolCallQuery("org/a", "exec", "python work.py", 100)) == {}


def test_completed_error_and_timeout_text_are_not_censored(tmp_path):
    records = [json.loads(line) for line in trace("org__a-1", "python timeout_test.py").splitlines()]
    data = records[1]["data"]
    data.update(success=False, error_type="ProcessExitError", tool_result="cancel timeout abort")
    path = tmp_path / "error.trace.jsonl"
    path.write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
    loaded = read_task(path, repo="org/a", task_id="org__a-1", rss_unit="MB", trust_call_cgroup=True)
    assert not loaded.calls[0].censored and len(loaded.clauses) == 1
    runtime = RuntimeToolResourceKB.fit_public(loaded.calls)
    targets = runtime.predict_load_samples(ToolCallQuery("org/a", "exec", "python timeout_test.py", 100))
    assert targets["duration_ms"]["values"] == (1000,)
    assert targets["cpu_time_seconds"]["values"] == (2,)


def test_exported_snapshots_exclude_timeout_labels(tmp_path):
    root = tmp_path / "dataset"
    root.mkdir()
    records = [json.loads(line) for line in trace("org__single-1").splitlines()]
    timed_out = json.loads(json.dumps(records[1]))
    timed_out["action_id"] = "tool-2"
    timed_out["data"].update(duration_ms=60000, success=False, error_type="timeout")
    records.append(timed_out)
    (root / "org__single-1.trace.jsonl").write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
    out = tmp_path / "seed"
    report = export(root, build_manifest(root), out, rss_unit="MB", trust_call_cgroup=True)
    assert report["counts"]["censored_calls"] == 1
    payloads = [json.loads((out / name).read_text()) for name in FILENAMES]
    trie, runtime, lattice = (ClauseResourceKB.from_json_obj(payloads[0]),
                              RuntimeToolResourceKB.from_json_obj(payloads[1]),
                              LatticeTimeKB.from_json_obj(payloads[2]))
    targets = runtime.predict_load_samples(ToolCallQuery("org/single", "exec", "python work.py", 100))
    assert targets["duration_ms"]["values"] == (1000,)
    assert targets["cpu_time_seconds"]["values"] == (2,)
    for kb in (trie, lattice):
        samples = kb.predict_load_samples("org/single", [{"bin": "python", "argv": ["python", "work.py"]}], 100)
        assert samples[0]["duration_ms"]["values"] == (1000,)


def test_export_is_train_only_and_every_backend_freezes_without_mutation(tmp_path):
    root = dataset(tmp_path)
    manifest = build_manifest(root)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    out = tmp_path / "seed"
    report = export(root, manifest, out, rss_unit="MB")
    assert report["counts"]["calls"] == 6  # duplicate identical file not double counted
    assert report["train_tasks"] == manifest["train_tasks"]
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    payloads = [json.loads((out / name).read_text()) for name in FILENAMES]
    assert all(not p["pending"] and p["last_query_ts"] is None for p in payloads)
    assert payloads[0]["repo"] and payloads[1]["repo"]
    clause, runtime, lattice = ClauseResourceKB.from_json_obj(payloads[0]), RuntimeToolResourceKB.from_json_obj(payloads[1]), LatticeTimeKB.from_json_obj(payloads[2])
    for kb in (clause, runtime, lattice):
        kb.freeze()
    originals = [kb.to_json_obj() for kb in (clause, runtime, lattice)]
    original_observation = ClauseObservation("org/a", "python", ("python", "work.py"), 0, 1, latency_ms=999999)
    for ts in (100, 1, 50, -1):
        query = ToolCallQuery("org/a", "exec", "python work.py", ts)
        assert runtime.predict_load_samples(query)["duration_ms"]["values"]
        runtime.observe_completed_call(CompletedCall("org/a", "exec", "python work.py", 0, 9999))
        for kb in (clause, lattice):
            assert kb.predict_load_samples("org/a", [{"bin": "python", "argv": ["python", "work.py"]}], ts)
            kb.observe_completed_clause(original_observation)
    assert [kb.to_json_obj() for kb in (clause, runtime, lattice)] == originals
    with pytest.raises(ValueError, match="read-only"):
        export(root, manifest, root / "out", rss_unit="MB")


def test_frozen_sidecar_ignores_trace_paths_and_never_rewrites_seed(tmp_path):
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    root = dataset(tmp_path)
    out = tmp_path / "seed"
    export(root, build_manifest(root), out, rss_unit="MB")
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    predictor = ToolResourcePredictor.from_traces(openclaw_trace_paths=[tmp_path / "must-not-read"],
        ebpf_trace_paths=[tmp_path / "must-not-read-either"], artifact_dir=out,
        buckets=LatencyBuckets((100, 500, 2000)), repo="org/a", frozen=True)
    assert predictor.frozen and predictor.report.openclaw_traces_seen == 0
    predictor._flush_kb_batch((ClauseObservation("org/a", "x", ("x",), 0, 1, latency_ms=1000),))
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    (out / FILENAMES[0]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        ToolResourcePredictor.from_traces(openclaw_trace_paths=[], ebpf_trace_paths=[], artifact_dir=out,
            buckets=LatencyBuckets((100, 500, 2000)), repo="org/a", frozen=True)
