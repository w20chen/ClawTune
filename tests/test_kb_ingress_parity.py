"""Regression coverage for partial resource labels and online/replay parity."""
import json
from dataclasses import replace

import pytest

from tool_resource.sdk import _observations_from_call
from test_cold_start import trace
from cold_start.flat_loader import read_task
from offline.runner import _call_actuals, load_task, run
from clawtune_kb.store import digest
from tool_resource.runtime_kb import CompletedCall


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


@pytest.mark.parametrize("metric,field", [("cpu_time", "cpu_ns_cumulative"), ("cpu", "cpu_peak_cores"), ("memory", "sampled_peak_rss_mb")])
def test_unavailable_target_does_not_discard_other_labels(metric, field):
    row = clause()
    row["availability"][metric] = "insufficient_samples"
    parsed = parse(row)[0]
    assert getattr(parsed, field) is None
    assert parsed.latency_ms == 1000


@pytest.mark.parametrize("changes,accepted", [({}, True), ({"ts_end": None}, False),
    ({"status": {"state": "running"}}, False), ({"eligible_for_kb": False}, False),
    ({"availability": {"latency": "protocol_timeout", "cpu_time": "ok"}}, False)])
def test_partial_resource_requires_completion_and_valid_clocks(changes, accepted):
    row = clause()
    row["availability"]["latency"] = "unavailable"
    row.update(changes)
    result = parse(row)
    assert bool(result) == accepted
    if result:
        assert result[0].latency_ms is None
        assert result[0].cpu_ns_cumulative == 500_000_000


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
    missing = _call_actuals(replace(call, workload_duration_seconds=None))
    assert "duration_ms" not in missing and "cpu_avg_cores" not in missing
    assert missing["cpu_time_seconds"] == .25


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
        assert counters["duration_ms"] == {"queries": 1, "labeled": 1, "predicted": 1, "scored": 1}
        assert counters["memory_total_peak_bytes"]["labeled"] == 0
        assert counters["memory_total_peak_bytes"]["scored"] == 0
        if name == "edge_kappa":
            assert counters["cpu_time_seconds"]["predicted"] == 0
            assert counters["cpu_time_seconds"]["scored"] == 0
        elif name != "tool":
            assert counters["cpu_time_seconds"]["scored"] == 1
            metric = next(m for m in model["metrics"] if m["target"] == "cpu_time_seconds")
            assert metric["mae"] == 0
    rows = [json.loads(line) for line in (tmp_path / "output" / "model-predictions.jsonl").read_text().splitlines()]
    assert {row["model"] for row in rows} == {"tool", "trie", "lattice", "edge_kappa"}


@pytest.mark.parametrize("censored", [False, True])
def test_v6_offline_compact_cpu_rss_and_censor_gate(tmp_path, censored):
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
    assert len(loaded.clauses) == (0 if censored else 1)
    if not censored:
        assert loaded.clauses[0].cpu_ns_cumulative == 500_000_000
        assert loaded.clauses[0].sampled_peak_rss_mb == 8
        assert loaded.call_actuals[0]["duration_ms"] == 1000


def test_missing_workload_does_not_reuse_tool_elapsed(tmp_path):
    rows = [json.loads(line) for line in trace("org__repo-1").splitlines()]
    rows[1]["data"].pop("resource_observation")
    path = tmp_path / "v5.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    loaded = read_task(path, repo="repo", task_id="org__repo-1", rss_unit="MB")
    assert loaded.calls[0].ts_end == 1
    assert "duration_ms" not in _call_actuals(loaded.calls[0])
    assert loaded.call_actuals == [{}]


def test_zero_cpu_is_a_valid_label_but_invalid_cpu_is_missing():
    row = clause()
    row["provenance"]["cpu_time_ns"] = 0
    assert parse(row)[0].cpu_ns_cumulative == 0
    for value in (-1, True, float("nan")):
        row["provenance"]["cpu_time_ns"] = value
        assert parse(row)[0].cpu_ns_cumulative is None
