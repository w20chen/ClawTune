from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.contracts.load_prediction import CallLoadPrediction, TARGET_UNITS
from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import compose, plain_execution, predict_call_load, summarize
from clawtune_sidecar.policies.concurrency import _predicted_cpu_millis
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, CompletedCall, RuntimeToolResourceKB, ToolCallQuery
from tool_time.lattice_kb import LatticeTimeKB

EDGES = load_bucket_edges((100, 500, 2000, 10000))


@pytest.fixture(autouse=True)
def parser_response_fixture(monkeypatch):
    # Explicit clause spans exercise composer structure checks without the
    # native parser. Native parsing remains a separate Linux integration check.
    def parse(command):
        clauses = []
        for match in re.finditer(r"python job\.py|sleep 1|cat|cd /tmp", command):
            argv = match.group().split()
            clauses.append(dict(bin=argv[0], argv=argv, span=match.span(),
                                in_loop=False, in_pipe=False, in_subst=False))
        return {"clauses": clauses, "parse_failed": False, "control_edges": []}
    monkeypatch.setattr("clawtune_sidecar.predictors.call_load.parse_command_clauses", parse)


def row(i=0, **overrides):
    data = dict(repo="repo", bin="python", argv=("python", "job.py"), ts_start=float(i), ts_end=float(i + 1),
                latency_ms=1000, cpu_ns_cumulative=2_000_000_000, peak_cpu_cores=4, sampled_peak_rss_mb=64)
    return ClauseObservation(**(data | overrides))


def test_statistics_and_right_open_boundaries():
    result = summarize("cpu_avg_cores", (1, 2), "trie", [0, 1, 2, 3])
    assert (result.avg, result.p50, result.p90) == (1.5, 1.5, 3)
    assert result.buckets.probabilities == [.25, .25, .5]
    missing = summarize("cpu_avg_cores", (1, 2), "trie")
    assert missing.buckets.edges == [1, 2]
    assert missing.avg is None and missing.buckets.probabilities is None


@pytest.mark.parametrize("edges", [(), (0, 1), (2, 1), (1, 1), (float("nan"),), (float("inf"),), (True,)])
def test_invalid_bucket_configuration(edges):
    with pytest.raises(ValueError):
        load_bucket_edges((100, 500), {"cpu_avg_cores": edges})


def test_env_bucket_configuration(monkeypatch):
    monkeypatch.setenv("CLAWTUNE_TOOL_RESOURCE_CPU_AVG_BUCKETS_CORES", "0.25,1,3")
    monkeypatch.setenv("CLAWTUNE_TOOL_RESOURCE_LATENCY_BUCKETS_MS", "50,300")
    cfg = SidecarConfig.from_env()
    assert cfg.tool_resource_load_buckets["cpu_avg_cores"] == (.25, 1, 3)
    assert cfg.tool_resource_latency_buckets_ms == (50, 300)


@pytest.mark.parametrize("command", ["python job.py && sleep 1", "python job.py | cat", "python job.py &", "(python job.py)",
                                     "for x in 1 2; do python job.py; done", "echo $(python job.py)", "cd /tmp; python job.py",
                                     "X=1 python job.py", "python job.py > out", "if true; then python job.py; fi"])
def test_unsupported_structures_are_explicit(command):
    assert plain_execution(command)[1] is not None


def test_serial_duration_is_distribution_not_sum_of_quantiles():
    assert plain_execution("python job.py; sleep 1")[1] is None
    evidence = [{"duration_ms": {"values": [0] * 9 + [100]}}] * 2
    result = compose("trie", evidence, EDGES)
    duration = result.targets["duration_ms"]
    assert duration.p90 == 100  # individual p90s are zero; their sum is wrong
    assert duration.evidence_counts == [10, 10]
    assert duration.sample_count == 2048
    assert "independent_clause_durations" in duration.assumptions
    assert result == compose("trie", evidence, EDGES)
    assert result.targets["cpu_peak_cores"].status == "unavailable"


def test_multi_clause_resources_not_added_even_with_evidence():
    evidence = [{t: {"values": [1, 2]} for t in TARGET_UNITS}] * 2
    result = compose("trie", evidence, EDGES)
    for target in set(TARGET_UNITS) - {"duration_ms"}:
        assert result.targets[target].unavailable_reason == "requires_joint_execution_ownership_and_timeline"


def test_all_backends_full_targets_and_schema():
    rows = [row(i) for i in range(3)]
    trie, lattice = ClauseResourceKB.fit_public(rows), LatticeTimeKB.fit(rows)
    runtime = RuntimeToolResourceKB()
    runtime.observe_completed_call(CompletedCall("repo", "exec", "python job.py", 0, 1,
        cpu_time_seconds=2, cpu_time_eligible=True, peak_cpu_cores=4, peak_cpu_cores_eligible=True,
        cpu_peak_window_ms=500, memory_peak_rss_bytes=64 * 1024**2,
        memory_rss_eligible=True, memory_metric="sampled_distinct_mm_rss"))
    result, diagnostics = predict_call_load(runtime=runtime, trie=trie, lattice=lattice,
        query=ToolCallQuery("repo", "exec", "python job.py", 10), edges=EDGES)
    assert all(v.backend == "runtime" for v in result.targets.values())
    root = Path(__file__).resolve().parents[3]
    schemas = [json.loads(p.read_text()) for p in (root / "contracts").glob("*.schema.json")]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    schema = next(s for s in schemas if s["$id"].endswith("/call-load.schema.json"))
    for prediction in [result, *diagnostics.backends.values()]:
        assert set(prediction.targets) == set(TARGET_UNITS)
        assert all(t.status == "available" for t in prediction.targets.values())
        assert prediction.targets["cpu_avg_cores"].avg == 2
        assert prediction.targets["memory_peak_rss_bytes"].p90 == 64 * 1024**2
        Draft202012Validator(schema, registry=registry).validate(prediction.model_dump())
        assert CallLoadPrediction.model_validate(prediction.model_dump()) == prediction


def test_average_cpu_uses_paired_observation_not_ratio_of_marginals():
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall("r", "read", None, 0, 1, cpu_time_seconds=2, cpu_time_eligible=True))
    kb.observe_completed_call(CompletedCall("r", "read", None, 2, 12, cpu_time_seconds=2, cpu_time_eligible=True))
    values = kb.predict_load_samples(ToolCallQuery("r", "read", None, 20))["cpu_avg_cores"]["values"]
    assert values == (2, .2)
    assert summarize("cpu_avg_cores", (1, 2), "runtime", values).avg == 1.1


def test_runtime_independent_targets_causality_and_snapshot():
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall("r", "read", None, 0, 1, cpu_time_seconds=0, cpu_time_eligible=True))
    assert kb.predict_load_samples(ToolCallQuery("r", "read", None, 1)) == {}
    values = kb.predict_load_samples(ToolCallQuery("r", "read", None, 2))
    assert values["cpu_time_seconds"]["values"] == (0,)
    assert "cpu_peak_cores" not in values and "memory_peak_rss_bytes" not in values
    restored = RuntimeToolResourceKB.from_json_obj(kb.to_json_obj())
    assert restored.predict_load_samples(ToolCallQuery("r", "read", None, 2)) == values
    with pytest.raises(ValueError):
        restored.predict_load_samples(ToolCallQuery("r", "read", None, 1))


def test_v1_cpu_labels_quarantined():
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall("r", "read", None, 0, 1, peak_cpu_cores=2, peak_cpu_cores_eligible=True))
    data = kb.to_json_obj()
    data["schema"] = "runtime_tool_resource_kb_v1"
    restored = RuntimeToolResourceKB.from_json_obj(data)
    result = restored.predict_load_samples(ToolCallQuery("r", "read", None, 2))
    assert set(result) == {"duration_ms"}
    assert restored.to_json_obj()["schema"] == "runtime_tool_resource_kb_v2"


def test_loop_samples_not_reused_as_standalone():
    rows = [row(in_loop=True)]
    for kb in (ClauseResourceKB.fit_public(rows), LatticeTimeKB.fit(rows)):
        assert kb.predict_load_samples("repo", [{"bin": "python", "argv": ["python", "job.py"]}], 3) == ({},)


def test_resource_only_training_and_per_target_fault_isolation(monkeypatch):
    rows = [row(latency_ms=None, sampled_peak_rss_mb=0)]
    clause = [{"bin": "python", "argv": ["python", "job.py"]}]
    for kb in (ClauseResourceKB.fit_public(rows), LatticeTimeKB.fit(rows)):
        result = kb.predict_load_samples("repo", clause, 3)[0]
        assert "duration_ms" not in result and "cpu_avg_cores" not in result
        assert result["cpu_time_seconds"]["values"] == (2,)
        assert result["memory_peak_rss_bytes"]["values"] == (0,)
    lattice = LatticeTimeKB.fit([row()])
    lattice.prepare()
    def fail(*args, **kwargs):
        raise ValueError("bad resource state")
    monkeypatch.setattr(lattice._resource_states["load:cpu_peak_cores"], "predict", fail)
    result = lattice.predict_load_samples("repo", clause, 3)[0]
    assert result["duration_ms"]["values"] == (1000,)
    assert result["cpu_peak_cores"]["unavailable_reason"] == "target_error:ValueError"


def test_policy_uses_only_call_targets_and_zero_is_valid():
    result = compose("trie", [{"cpu_avg_cores": {"values": [1.2341]}}], EDGES)
    assert _predicted_cpu_millis(result) == 1235
    zero = compose("trie", [{"cpu_avg_cores": {"values": [0]}}], EDGES)
    assert _predicted_cpu_millis(zero) == 1
    assert _predicted_cpu_millis({"continuous_predictions": {"peak_cpu_cores": {"conditional_p90": 100}}}) == 1000


def test_censored_resource_totals_not_used_as_complete_load():
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall("r", "read", None, 0, 1, censored=True,
        cpu_time_seconds=2, cpu_time_eligible=True, peak_cpu_cores=4, peak_cpu_cores_eligible=True,
        cpu_peak_window_ms=500, memory_peak_rss_bytes=100, memory_rss_eligible=True,
        memory_metric="sampled_distinct_mm_rss", outcome="timeout"))
    assert kb.predict_load_samples(ToolCallQuery("r", "read", None, 2)) == {}


def test_call_level_evaluation_scores_and_metric_guard():
    from clawtune_sidecar.predictors.call_load_eval import evaluate_calls
    prediction = compose("trie", [{"duration_ms": {"values": [100, 200]}}], EDGES).model_dump()
    record = dict(prediction=prediction, scope="tool_call", lifecycle="tool_hook_interval",
                  actual={"duration_ms": {"valid": True, "value": 200, "metric_definition": "tool_hook_elapsed"}})
    result = evaluate_calls([record])["targets"]["duration_ms"]
    assert result["availability"] == 1
    assert result["avg_absolute_error"] == 50
    assert result["p90_coverage"] == 1 and result["p90_pinball_loss"] == 0
    record["actual"]["duration_ms"]["metric_definition"] = "clause_duration"
    with pytest.raises(ValueError, match="incompatible"):
        evaluate_calls([record])


def test_prepared_successor_does_not_mutate_published_generation():
    published = LatticeTimeKB.fit([row()])
    published.prepare()
    successor = published.fork_for_update()
    successor.observe_completed_clause(row(2, latency_ms=3000))
    successor.prepare()
    clause = [{"bin": "python", "argv": ["python", "job.py"]}]
    assert published.predict_load_samples("repo", clause, 10)[0]["duration_ms"]["values"] == (1000,)
    assert sorted(successor.predict_load_samples("repo", clause, 10)[0]["duration_ms"]["values"]) == [1000, 3000]


def test_background_generation_does_not_hold_prediction_lock(monkeypatch):
    import threading
    from tool_resource.runtime_kb import LatencyBuckets
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    predictor = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
        buckets=LatencyBuckets((100, 500, 2000)), repo="repo")
    entered, release = threading.Event(), threading.Event()
    prepare = LatticeTimeKB.prepare
    def paused_prepare(self):
        entered.set()
        assert release.wait(5)
        prepare(self)
    monkeypatch.setattr(LatticeTimeKB, "prepare", paused_prepare)
    errors = []
    def update():
        try:
            predictor._flush_kb_batch((row(),))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=update)
    thread.start()
    try:
        assert entered.wait(2)
        acquired = predictor._kb_lock.acquire(timeout=.2)
        assert acquired
        if acquired:
            predictor._kb_lock.release()
        assert predictor.lattice_kb.observation_count == 0
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
