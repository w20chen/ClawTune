"""Regression coverage for partial resource labels and online/replay parity."""
import json
from dataclasses import replace

import pytest

from tool_resource.sdk import _observations_from_call
from test_cold_start import trace
from cold_start.flat_loader import read_task
from offline.runner import _call_actuals, load_task, run
from clawtune_kb.store import digest
from tool_resource.runtime_kb import CompletedCall, ClauseResourceKB, training_target_provenance
from tool_time.lattice_kb import LatticeTimeKB
from clawtune_sidecar.predictors.tool_resource import (
    STRICT_TRACE_LABEL_POLICY, apply_trace_label_policy_to_clause,
)


def clause():
    return {"bin": "python", "argv": ["python", "work.py"],
            "ts_start": 1., "ts_end": 2., "latency_ms": 1000.,
            "status": {"state": "exited"},
            "availability": {"latency": "ok", "cpu_time": "ok", "cpu": "ok", "memory": "ok"},
            "cpu_ns_cumulative": 9_000_000_000, "provenance": {"cpu_time_ns": 500_000_000},
            "peak_cpu_cores": 2., "sampled_peak_rss_mb": 8.}


def parse(row):
    return _observations_from_call("repo", {"command": "python work.py", "clauses": [row]}, require_timestamps=False)


def test_cpu_uses_interval_evidence_and_compact_rss_alias():
    raw = clause()
    first = parse(raw)[0]
    assert first.cpu_ns_cumulative == 500_000_000
    compact = dict(raw)
    compact.pop("provenance")
    compact["cpu_time_seconds"] = .5
    compact["peak_memory_mb"] = compact.pop("sampled_peak_rss_mb")
    assert parse(compact)[0] == first
    compact.pop("cpu_time_seconds")
    assert parse(compact)[0].cpu_ns_cumulative is None


@pytest.mark.parametrize("metric,field,value,target", [
    ("cpu_time", "cpu_ns_cumulative", 500_000_000, "cpu_time_seconds"),
    ("cpu", "cpu_peak_cores", 2., "cpu_peak_cores"),
    ("memory", "sampled_peak_rss_mb", 8., "sampled_peak_rss_bytes"),
])
def test_approximate_target_retains_observed_value_and_only_missing_is_imputed(metric, field, value, target):
    row = clause()
    row["availability"][metric] = "insufficient_samples"
    parsed = parse(row)[0]
    assert getattr(parsed, field) == value
    assert parsed.latency_ms == 1000
    retained = apply_trace_label_policy_to_clause(parsed, STRICT_TRACE_LABEL_POLICY)
    assert getattr(retained, field) == value
    assert target not in retained.imputed_targets
    missing = apply_trace_label_policy_to_clause(replace(parsed, **{field: None}), STRICT_TRACE_LABEL_POLICY)
    assert getattr(missing, field) == 0
    assert target in missing.imputed_targets
    assert missing.latency_ms == retained.latency_ms == 1000


@pytest.mark.parametrize("changes", [{}, {"ts_end": None},
    {"status": {"state": "running"}}, {"eligible_for_kb": False},
    {"availability": {"latency": "protocol_timeout", "cpu_time": "ok"}},
    {"status": {"state": "signaled"}}])
def test_partial_resource_keeps_values_but_requires_valid_recorded_clocks(changes):
    row = clause()
    row["availability"]["latency"] = "unavailable"
    row.update(changes)
    if "ts_end" in changes:
        with pytest.raises(ValueError, match="invalid ts_end"):
            parse(row)
        return
    result = parse(row)
    assert len(result) == 1
    assert result[0].latency_ms == 1000
    assert result[0].cpu_ns_cumulative == 500_000_000
    assert result[0].cpu_peak_cores == 2
    assert result[0].sampled_peak_rss_mb == 8
    assert result[0].censored == (row["status"]["state"] == "signaled")
    assert not result[0].imputed_targets


@pytest.mark.parametrize("unit,scale", [("MB", 1_000_000), ("MiB", 1024**2)])
def test_v5_cpu_scope_rss_units_and_workload_denominator(tmp_path, unit, scale):
    rows = [json.loads(line) for line in trace("org__repo-1").splitlines()]
    data = rows[1]["data"]
    data["duration_ms"] = 2000
    row = data["resource_observation"]["clauses"][0]
    row["sampled_peak_rss_mb"] = 8
    row["cpu_ns_cumulative"] = 9_000_000_000
    row["provenance"]["cpu_time_ns"] = 500_000_000
    path = tmp_path / "v5.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    loaded = read_task(path, repo="repo", task_id="org__repo-1", rss_unit=unit)
    assert loaded.clauses[0].sampled_peak_rss_mb * 1_000_000 == pytest.approx(8 * scale)
    assert loaded.call_actuals[0]["duration_ms"] == 1000
    assert loaded.call_actuals[0]["cpu_time_seconds"] == .5
    assert loaded.call_actuals[0]["cpu_avg_cores"] == .5
    assert _call_actuals(loaded.calls[0])["duration_ms"] == 1000
    trusted = read_task(path, repo="repo", task_id="org__repo-1", rss_unit=unit, trust_call_cgroup=True)
    assert not trusted.calls[0].cpu_time_eligible
    row.pop("provenance")
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    legacy = read_task(path, repo="repo", task_id="org__repo-1", rss_unit=unit)
    assert legacy.clauses[0].cpu_ns_cumulative is None
    assert "cpu_time_seconds" not in legacy.call_actuals[0]


def test_call_actuals_preserve_workload_scope_and_missing_values():
    call = CompletedCall("repo", "exec", "python work.py", 0., 2.,
                         workload_duration_seconds=.5, workload_duration_required=True,
                         cpu_time_seconds=.25, cpu_time_eligible=True)
    assert _call_actuals(call)["duration_ms"] == 500
    assert _call_actuals(call)["cpu_avg_cores"] == .5
    missing_call = replace(call, workload_duration_seconds=None)
    missing = _call_actuals(missing_call)
    assert missing["duration_ms"] == missing["cpu_avg_cores"] == 0
    assert missing["cpu_time_seconds"] == .25
    provenance = training_target_provenance(missing_call)
    assert provenance["workload_latency_ms"] == provenance["cpu_avg_cores"] == "imputed_zero"
    assert provenance["cpu_time_seconds"] == "observed"
    assert provenance["latency_ms"] == "observed"


def test_offline_scores_each_model_and_reports_missing_labels(tmp_path):
    dataset = tmp_path / "input"
    dataset.mkdir()
    for index in range(5):
        task = f"org__repo-{index}"
        (dataset / f"{task}.trace.jsonl").write_text(trace(task), encoding="utf-8")
    report = run(dataset, tmp_path / "output", benchmark="swe-rebench", rss_unit="MB", split_cache_dir=tmp_path / "splits")
    assert report["test_updates"] == 0
    for name, model in report["models"].items():
        counters = model["availability"]
        assert counters["duration_ms"] == {"queries": 1, "labeled": 1, "predicted": 1,
                                           "scored": 1, "unavailable_reasons": {}}
        assert model["repositories"][0]["repo"] == "org/repo"
        assert counters["memory_total_peak_bytes"]["labeled"] == 1
        assert counters["memory_total_peak_bytes"]["scored"] == 1
        memory_metric = next(m for m in model["metrics"] if m["target"] == "memory_total_peak_bytes")
        assert memory_metric["mae"] == 0
        if name != "tool":
            assert counters["cpu_time_seconds"]["scored"] == 1
            metric = next(m for m in model["metrics"] if m["target"] == "cpu_time_seconds")
            assert metric["mae"] == 0
    rows = [json.loads(line) for line in (tmp_path / "output" / "model-predictions.jsonl").read_text().splitlines()]
    assert {row["model"] for row in rows} == {"tool", "trie", "lattice"}
    assert all(row["tool"] in {"exec", "terminal_exec"} for row in rows)
    assert all(row["actual"] == 0 for row in rows if row["target"] == "memory_total_peak_bytes")


def test_static_import_projects_recorded_repo_features_without_reparsing():
    from offline.runner import _static_clause
    from command_parser.models import ParseContext
    from command_parser.service import default_service, observation_features
    from tool_resource.runtime_kb import ClauseObservation
    record = default_service().argv(("python", "work.py"), ParseContext(repo="org/repo"))
    row = ClauseObservation("org/repo", "python", ("python", "work.py"), 10., 11.,
                            latency_ms=1000., command_features=record.to_dict())
    imported = _static_clause(row, "swe-rebench:org/repo")
    result = observation_features(imported)
    assert 'context:["repo","swe-rebench:org/repo"]' in result.features
    assert 'context:["repo","org/repo"]' not in result.features
    assert result.core == record.core and result.parse_digest == record.parse_digest
    assert imported.latency_ms == row.latency_ms and imported.ts_end == 1.
    assert observation_features(row) == record


@pytest.mark.parametrize("measurement", [
    "cgroup_v2_environment_union_v1", "cgroup_v2_memory_current",
    "guest_memtotal_minus_memavailable",
])
def test_offline_memory_query_uses_the_recorded_measurement(tmp_path, monkeypatch, measurement):
    dataset = tmp_path / "input"
    dataset.mkdir()
    for index in range(5):
        task = f"org__repo-{index}"
        (dataset / f"{task}.trace.jsonl").write_text(trace(task), encoding="utf-8")
    fields = dict(memory_baseline_bytes=100, memory_total_peak_bytes=300,
                  memory_extra_peak_bytes=200, memory_measurement=measurement,
                  memory_environment_id="test", memory_eligible=True)

    def load_with_memory(*args, **kwargs):
        loaded = load_task(*args, **kwargs)
        loaded.calls = [replace(call, **fields) for call in loaded.calls]
        loaded.clauses = [replace(row, **fields) for row in loaded.clauses]
        return loaded

    monkeypatch.setattr("offline.runner.load_task", load_with_memory)
    report = run(dataset, tmp_path / "output", benchmark="swe-rebench", rss_unit="MB",
                 split_cache_dir=tmp_path / "splits")
    for model in ("tool", "trie", "lattice"):
        for target in ("memory_total_peak_bytes", "memory_extra_peak_bytes"):
            metric = next(row for row in report["models"][model]["metrics"]
                          if row["target"] == target)
            assert metric["coverage"] == 1
            assert metric["mae"] == 0


@pytest.mark.parametrize("censored", [False, True])
def test_v6_offline_compact_cpu_rss_and_partial_labels(tmp_path, censored):
    row = clause()
    row.pop("provenance")
    row["cpu_time_seconds"] = .5
    row["peak_memory_mb"] = row.pop("sampled_peak_rss_mb")
    end = {"record_type": "span_end", "kind": "tool", "span_id": "call", "name": "exec",
           "wall_time_ns": "2000000000", "duration_ns": "2000000000",
           "status": {"code": "error" if censored else "ok", "message": "timeout" if censored else None},
           "execution": {"execution_id": "call", "tool_resource": {"call_telemetry": {
               "command": "python work.py", "eligible_for_kb": True, "telemetry_quality": "ok", "clauses": [row]}}}}
    path = tmp_path / "v6.jsonl"
    path.write_text(json.dumps(end), encoding="utf-8")
    task = {"benchmark": "swe-rebench", "group": "org/repo", "task_id": "task",
            "files": [{"path": path.name, "sha256": digest(path), "version": 6}]}
    loaded = load_task(tmp_path, task, "MB")
    assert len(loaded.clauses) == 1
    observed = loaded.clauses[0]
    assert observed.cpu_ns_cumulative == 500_000_000
    assert observed.cpu_peak_cores == 2
    assert observed.sampled_peak_rss_mb == 8
    assert observed.latency_ms == loaded.call_actuals[0]["duration_ms"] == 1000
    assert observed.censored == loaded.calls[0].censored == censored
    assert observed.memory_total_peak_bytes == observed.memory_extra_peak_bytes == 0
    assert set(observed.imputed_targets) == {"memory_total_peak_bytes", "memory_extra_peak_bytes"}
    # Clause-owned CPU is not silently promoted to a whole-call measurement.
    assert loaded.call_actuals[0]["cpu_time_seconds"] == 0
    provenance = training_target_provenance(loaded.calls[0])
    assert provenance["cpu_time_seconds"] == "imputed_zero"
    assert provenance["workload_latency_ms"] == ("observed_partial" if censored else "observed")
    for kb in (ClauseResourceKB(), LatticeTimeKB()):
        assert kb.observe_completed_clause(observed)
        evidence = kb.predict_load_samples(observed.repo, [{"bin": observed.bin, "argv": list(observed.argv)}], 3.)[0]
        for target, value in (("duration_ms", 1000.), ("cpu_time_seconds", .5),
                              ("cpu_peak_cores", 2.), ("sampled_peak_rss_bytes", 8_000_000.),
                              ("memory_total_peak_bytes", 0.)):
            assert evidence[target]["values"] == (value,)


def test_missing_workload_does_not_reuse_tool_elapsed(tmp_path):
    rows = [json.loads(line) for line in trace("org__repo-1").splitlines()]
    rows[1]["data"].pop("resource_observation")
    path = tmp_path / "v5.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    loaded = read_task(path, repo="repo", task_id="org__repo-1", rss_unit="MB")
    assert loaded.calls[0].ts_end == 1
    call = loaded.calls[0]
    actuals = _call_actuals(call)
    assert actuals["duration_ms"] == 0
    assert actuals["cpu_time_seconds"] == 2
    provenance = training_target_provenance(call)
    assert provenance["workload_latency_ms"] == "imputed_zero"
    assert provenance["cpu_time_seconds"] == "approximate_observation"
    assert loaded.call_actuals == [{}]


def test_zero_cpu_is_a_valid_label_but_invalid_cpu_is_missing():
    row = clause()
    row["provenance"]["cpu_time_ns"] = 0
    observed_zero = apply_trace_label_policy_to_clause(parse(row)[0], STRICT_TRACE_LABEL_POLICY)
    assert observed_zero.cpu_ns_cumulative == 0
    assert "cpu_time_seconds" not in observed_zero.imputed_targets
    for value in (-1, True, float("nan")):
        row["provenance"]["cpu_time_ns"] = value
        invalid = parse(row)[0]
        assert invalid.cpu_ns_cumulative is None
        imputed = apply_trace_label_policy_to_clause(invalid, STRICT_TRACE_LABEL_POLICY)
        assert imputed.cpu_ns_cumulative == 0
        assert "cpu_time_seconds" in imputed.imputed_targets
        assert imputed.cpu_peak_cores == 2 and imputed.sampled_peak_rss_mb == 8
