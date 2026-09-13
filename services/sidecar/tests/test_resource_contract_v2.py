from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest
from jsonschema import Draft202012Validator

from clawtune_sidecar.monitoring.environment_memory import (
    EnvironmentMemoryMonitor, environment_memory, memory_labels, clause_memory_labels,
)
from clawtune_sidecar.monitoring.tool_runtime import _windowed_cpu_peak
from clawtune_sidecar.contracts.load_prediction import summarize_pmu_evidence, PMU_UNITS
from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import plain_execution, predict_call_load, compose
from tool_resource.runtime_kb import CompletedCall, ClauseObservation, RuntimeToolResourceKB, ClauseResourceKB, ToolCallQuery
from tool_time.lattice_kb import LatticeTimeKB

EDGES = load_bucket_edges((100, 1000, 10000))


def labels(source="cgroup_v2_memory_current"):
    return environment_memory(baseline=100, values=[100, 300, 200], environment_id="task-a",
        measurement=source, baseline_before_start=True, exclusive=True).fields()


def observation(i=0, **kwargs):
    return ClauseObservation(repo="r", bin="pytest", argv=("pytest", "tests/"),
        ts_start=i * 2, ts_end=i * 2 + 1, latency_ms=1000,
        cpu_ns_cumulative=2_000_000_000, cpu_peak_cores=3, **(labels() | kwargs))


def test_memory_means_total_and_increment_not_rss():
    assert memory_labels(labels()) == {"memory_total_peak_bytes": 300, "memory_extra_peak_bytes": 200}
    assert memory_labels({"sampled_peak_rss_mb": 512, "memory_eligible": True}) == {}
    assert memory_labels(labels() | {"memory_extra_peak_bytes": 300}) == {}
    assert environment_memory(baseline=100, values=[300], environment_id="task-a",
        measurement="host_vm_rss", baseline_before_start=True, exclusive=True) is None


@pytest.mark.parametrize("kind", ["runtime", "trie", "lattice"])
def test_memory_sources_are_not_mixed(kind):
    if kind == "runtime":
        kb = RuntimeToolResourceKB()
        for source, value in [("cgroup_v2_memory_current", 300), ("guest_memtotal_minus_memavailable", 900)]:
            kb.observe_completed_call(CompletedCall("r", "exec", "pytest tests/", 0, 1,
                **(labels(source) | {"memory_total_peak_bytes": value, "memory_extra_peak_bytes": value - 100})))
        result = kb.predict_load_samples(ToolCallQuery("r", "exec", "pytest tests/", 10))
    else:
        rows = [observation(), observation(memory_measurement="guest_memtotal_minus_memavailable",
                                          memory_total_peak_bytes=900, memory_extra_peak_bytes=800)]
        kb = ClauseResourceKB.fit_public(rows) if kind == "trie" else LatticeTimeKB.fit(rows)
        result = kb.predict_load_samples("r", [{"bin": "pytest", "argv": ["pytest", "tests/"]}], 10)[0]
    assert result["memory_total_peak_bytes"]["values"] == (300,)


def test_overlap_never_duplicates_environment_peak_into_two_clauses():
    env = labels() | {"memory_timeline": [(0, 100), (.05, 150), (.1, 300), (.15, 200)]}
    assert clause_memory_labels(env, [{"ts_start": .01, "ts_end": .14},
                                      {"ts_start": .05, "ts_end": .15}]) == [{}, {}]
    one = clause_memory_labels(env, [{"ts_start": .01, "ts_end": .14}])[0]
    assert one["memory_extra_peak_bytes"] == 200


def test_eight_independent_environments_and_overlapping_calls(tmp_path):
    monitor = EnvironmentMemoryMonitor()
    from types import SimpleNamespace
    def worker(i):
        path = tmp_path / str(i); path.mkdir(); (path / "memory.current").write_text("100")
        monitor.begin(i, SimpleNamespace(cgroup_path=str(path)))
        (path / "memory.current").write_text(str(200 + i)); monitor.poll()
        return monitor.complete(i)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert [r["memory_extra_peak_bytes"] for r in results] == list(range(100, 108))
    scope = SimpleNamespace(cgroup_path=str(tmp_path / "0"))
    monitor.begin("a", scope); monitor.begin("b", scope)
    assert monitor.complete("a")["memory_eligible"] is False
    assert monitor.complete("b")["memory_eligible"] is False


def test_cpu_peak_requires_window_coverage():
    points = [{"ts": i / 20, "cpu_time_s": i / 10, "source": "cgroup-v2", "available": True}
              for i in range(25)]
    assert _windowed_cpu_peak(points) == pytest.approx(2)
    assert _windowed_cpu_peak(points[:8]) is None
    assert _windowed_cpu_peak(points[:5] + points[15:]) is None


def test_all_nine_pmu_targets_roundtrip():
    kb = RuntimeToolResourceKB()
    metrics = {"pmu_" + k: float(i + 1) for i, k in enumerate(PMU_UNITS)}
    metrics["pmu_llc_miss_rate"] = .2
    kb.observe_completed_call(CompletedCall("r", "exec", "pytest tests/", 0, 1, pmu_eligible=True, **metrics))
    assert kb.predict_pmu_samples(ToolCallQuery("r", "exec", "pytest tests/", 1)) == {}
    evidence = kb.predict_pmu_samples(ToolCallQuery("r", "exec", "pytest tests/", 2))
    assert len(evidence) == 9
    pred = summarize_pmu_evidence(evidence)
    assert all(v.status == "available" for v in pred.targets.values())
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts/pmu-prediction.schema.json").read_text())
    Draft202012Validator(schema).validate(pred.model_dump())
    restored = RuntimeToolResourceKB.from_json_obj(kb.to_json_obj())
    assert restored.predict_pmu_samples(ToolCallQuery("r", "exec", "pytest tests/", 2)) == evidence
    old = kb.to_json_obj() | {"schema": "runtime_tool_resource_kb_v2"}
    with pytest.raises(ValueError, match="unsupported"):
        RuntimeToolResourceKB.from_json_obj(old)


def test_both_tool_names_use_clauses_with_environment_preparation(monkeypatch):
    command = "cd /workspace && pytest tests/ | tail -20"
    def parse(_):
        return {"parse_failed": False, "control_edges": [], "clauses": [
            {"bin": "cd", "argv": ["cd", "/workspace"], "span": (0, 13)},
            {"bin": "pytest", "argv": ["pytest", "tests/"], "span": (17, 30), "in_pipe": True, "pipeline_position": 0},
            {"bin": "tail", "argv": ["tail", "-20"], "span": (33, 41), "in_pipe": True, "pipeline_position": 1},
        ]}
    monkeypatch.setattr("clawtune_sidecar.predictors.call_load.parse_command_clauses", parse)
    results = []
    for name in ("exec", "terminal_exec"):
        pred, _ = predict_call_load(runtime=RuntimeToolResourceKB(), trie=ClauseResourceKB.fit_public([observation()]),
            lattice=LatticeTimeKB.fit([observation()]), query=ToolCallQuery("r", name, command, 10), edges=EDGES)
        assert pred.targets["cpu_peak_cores"].p50 == 3
        assert pred.targets["memory_extra_peak_bytes"].p50 == 200
        assert pred.clause_predictions[0].cwd == "/workspace"
        results.append(pred)
    assert results[0] == results[1]


def test_two_work_clauses_cpu_sums_memory_not_fabricated():
    evidence = [{"duration_ms": {"values": [1000]}, "cpu_time_seconds": {"values": [2]},
                 "cpu_peak_cores": {"values": [3]}, "memory_total_peak_bytes": {"values": [300]}}] * 2
    result = compose("trie", evidence, EDGES)
    assert result.targets["cpu_time_seconds"].p50 == 4
    assert result.targets["cpu_peak_cores"].p50 == 3
    assert result.targets["memory_total_peak_bytes"].status == "unavailable"


def test_environment_is_polled_on_every_monitor_iteration():
    from types import SimpleNamespace
    from clawtune_sidecar.monitoring.tool_runtime import RealtimeToolMonitor
    waits = iter([False, False, False, True])
    polls = []
    monitor = RealtimeToolMonitor.__new__(RealtimeToolMonitor)
    monitor._stop = SimpleNamespace(wait=lambda _: next(waits))
    monitor.poll_interval_s = .05
    monitor._lock = threading.RLock()
    monitor._active = {}
    monitor.environment_memory = SimpleNamespace(poll=lambda: polls.append(1))
    monitor._poll_active()
    assert len(polls) == 3


def test_wrapped_training_matches_unwrapped_query_and_reimport():
    wrapped = replace(observation(), bin="timeout", argv=("timeout", "-k", "2", "30", "env", "X=1", "pytest", "tests/"))
    for kb in (ClauseResourceKB(), LatticeTimeKB()):
        kb.observe_completed_clause(wrapped)
        query = [{"bin": "pytest", "argv": ["pytest", "tests/"]}]
        assert kb.predict_load_samples("r", query, 10)[0]["cpu_peak_cores"]["values"] == (3,)
        restored = type(kb).from_json_obj(kb.to_json_obj())
        assert restored.merge_historical([wrapped]) == 0
        assert restored.predict_load_samples("r", query, 11)[0]["memory_extra_peak_bytes"]["values"] == (200,)


def test_eight_completions_keep_independent_toolkb_histories(tmp_path):
    from test_tool_resource_predictor import _tool_request, _tool_completion, _runtime_sample
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    from tool_resource.runtime_kb import LatencyBuckets
    predictor = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
        buckets=LatencyBuckets((100, 1000)), repo="r", artifact_dir=tmp_path / "kb")
    barrier = threading.Barrier(8)
    def complete(i):
        request = _tool_request(f"e{i}", f"c{i}", "pytest tests/").model_copy(update={"repo": f"repo-{i}"})
        event = _tool_completion(f"e{i}", f"c{i}").model_copy(update={"repo": f"repo-{i}"})
        sample = replace(_runtime_sample(f"e{i}", f"c{i}"), environment_memory=labels() | {
            "memory_total_peak_bytes": 300 + i, "memory_extra_peak_bytes": 200 + i})
        predictor.record_tool_started(request)
        barrier.wait(5)
        return predictor.observe_completion(event, sample)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(complete, range(8))) == [1] * 8
    predictor.flush_kb_updates(timeout_seconds=10)
    restored = RuntimeToolResourceKB.from_json_obj(predictor.continuous_kb.to_json_obj())
    for i in range(8):
        evidence = restored.predict_load_samples(ToolCallQuery(f"repo-{i}", "exec", "pytest tests/", 2000))
        assert evidence["memory_extra_peak_bytes"]["values"] == (200 + i,)
    predictor.close()


def test_environment_memory_contract():
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts/environment-memory.schema.json").read_text())
    Draft202012Validator(schema).validate(labels())


def test_live_clause_memory_reloads_from_trace_without_copying_cpu(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from test_tool_resource_predictor import _tool_request, _tool_completion, _runtime_sample
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor, load_openclaw_trace_observations
    from clawtune_sidecar.trace import AgentTestBenchTraceWriter
    from tool_resource.runtime_kb import LatencyBuckets
    predictor = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
        buckets=LatencyBuckets((100, 1000)), repo="r", artifact_dir=tmp_path / "kb")
    request = _tool_request("e", "c", "pytest tests/").model_copy(update={"repo": "r"})
    event = _tool_completion("e", "c").model_copy(update={"repo": "r", "execution_id": "x"})
    sample = replace(_runtime_sample("e", "c"), environment_memory=labels() | {
        "memory_timeline": [(1000, 100), (1000.1, 300), (1000.2, 200)]})
    artifact = {"calls": [{"eligible_for_kb": True, "clauses": [
        {"bin": "pytest", "argv": ["pytest", "tests/"], "ts_start": 1000.01, "ts_end": 1000.21}]}]}
    predictor._telemetry_by_execution_id["x"] = SimpleNamespace(artifact_path="test.json")
    monkeypatch.setattr("clawtune_sidecar.predictors.tool_resource._read_ebpf_artifact", lambda _: artifact)
    predictor.record_tool_started(request)
    predictor.observe_completion(event, sample)
    predictor.flush_kb_updates(timeout_seconds=10)
    assert not predictor.kb_updates_pending()
    assert len(sample.environment_memory["memory_clause_observations"]) == 1
    writer = AgentTestBenchTraceWriter(tmp_path / "traces")
    writer.record_tool_started(request)
    writer.record_tool(event, sample)
    assert writer.flush()
    writer.close()
    loaded = [load_openclaw_trace_observations(p, repo="r") for p in (tmp_path / "traces").glob("*.jsonl")]
    rows = [row for trace in loaded for row in trace.observations]
    assert len(rows) == 1 and rows[0].memory_extra_peak_bytes == 200
    assert rows[0].cpu_ns_cumulative is None and rows[0].latency_ms is None
    for kb in (ClauseResourceKB.fit_public(rows), LatticeTimeKB.fit(rows)):
        values = kb.predict_load_samples("r", [{"bin": "pytest", "argv": ["pytest", "tests/"]}], 2000)[0]
        assert values["memory_total_peak_bytes"]["values"] == (300,)
        assert "cpu_time_seconds" not in values
    predictor.close()


def test_trusted_root_pid_never_reads_shared_cgroup_cpu(tmp_path, monkeypatch):
    import os
    from clawtune_sidecar.monitoring.process import ProcessResourceSampler
    from clawtune_sidecar.contracts.models import ResourceScope
    sampler = ProcessResourceSampler()
    def forbidden(*args, **kwargs):
        pytest.fail("owned PID CPU was replaced with shared-container cpu.stat")
    monkeypatch.setattr(sampler, "_snapshot_cgroup", forbidden)
    scope = ResourceScope(kind="cgroup-v2", cgroup_path=str(tmp_path), root_pid=os.getpid(),
                          attribution_source="trusted-execution-root-pid")
    snapshot = sampler.snapshot(scope)
    assert snapshot.available and snapshot.source == "psutil-process-tree"
