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
        matches = list(re.finditer(r"python job\.py|sleep 1|cat|cd /tmp", command))
        pipeline = "|" in command
        for index, match in enumerate(matches):
            argv = match.group().split()
            clauses.append(dict(bin=argv[0], argv=argv, span=match.span(),
                                in_loop=False, in_pipe=pipeline, in_subst=False,
                                pipeline_position=index if pipeline else -1))
        return {"clauses": clauses, "parse_failed": False, "control_edges": []}
    monkeypatch.setattr("clawtune_sidecar.predictors.call_load.parse_command_clauses", parse)


def row(i=0, **overrides):
    data = dict(repo="repo", bin="python", argv=("python", "job.py"), ts_start=float(i), ts_end=float(i + 1),
                latency_ms=1000, cpu_ns_cumulative=2_000_000_000, cpu_peak_cores=4,
                sampled_peak_rss_mb=64 * 1024**2 / 1_000_000,
                memory_baseline_bytes=0, memory_total_peak_bytes=64 * 1024**2,
                memory_extra_peak_bytes=64 * 1024**2, memory_environment_id="test",
                memory_measurement="cgroup_v2_environment_union_v1", memory_eligible=True)
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


@pytest.mark.parametrize("command", ["python job.py && sleep 1", "cd /tmp; python job.py", "X=1 python job.py", "python job.py > out"])
def test_simple_preparation_and_conditionals_keep_executable_clauses(command):
    clauses, reason = plain_execution(command)
    assert reason is None and clauses


@pytest.mark.parametrize("command", ["python job.py &", "(python job.py)"])
def test_dynamic_structure_is_reported_per_clause(command):
    clauses, _ = plain_execution(command)
    assert clauses[0]["prediction_unavailable_reason"]


def test_serial_duration_is_distribution_not_sum_of_quantiles():
    assert plain_execution("python job.py; sleep 1")[1] is None
    evidence = [{"duration_ms": {"values": [0] * 9 + [100]}}] * 2
    result = compose("trie", evidence, EDGES)
    duration = result.targets["duration_ms"]
    assert duration.p90 == 100  # individual p90s are zero; their sum is wrong
    assert duration.evidence_counts == [10, 10]
    assert duration.sample_count == 2048
    assert "independent_clause_marginals" in duration.assumptions
    assert result == compose("trie", evidence, EDGES)
    assert result.targets["cpu_peak_cores"].status == "unavailable"


def test_pipeline_consumer_is_ignored_and_other_stages_use_max():
    clauses, reason = plain_execution("python job.py | cat")
    assert reason is None
    result = compose(
        "trie",
        [{"duration_ms": {"values": [100, 200]}}],
        EDGES,
        clauses=clauses,
    )
    assert result.targets["duration_ms"].p90 == 200

    clauses, reason = plain_execution("python job.py | sleep 1")
    assert reason is None
    evidence = [
        {"duration_ms": {"values": [100]}},
        {"duration_ms": {"values": [300]}},
    ]
    result = compose("trie", evidence, EDGES, clauses=clauses)
    assert result.targets["duration_ms"].p50 == 300
    assert "pipeline_group_duration_is_stage_max" in result.targets["duration_ms"].assumptions


def test_multi_clause_resources_not_added_even_with_evidence():
    evidence = [{t: {"values": [1, 2]} for t in TARGET_UNITS}] * 2
    result = compose("trie", evidence, EDGES)
    for target in ("cpu_time_seconds", "cpu_peak_cores"):
        assert result.targets[target].status == "available"
    assert result.targets["cpu_avg_cores"].unavailable_reason == "requires_paired_cpu_time_and_duration_samples"
    for target in ("memory_total_peak_bytes", "memory_extra_peak_bytes"):
        assert result.targets[target].unavailable_reason == "requires_environment_baseline_and_joint_memory_timeline"


def test_all_backends_full_targets_and_schema():
    rows = [row(i) for i in range(3)]
    trie, lattice = ClauseResourceKB.fit_public(rows), LatticeTimeKB.fit(rows)
    runtime = RuntimeToolResourceKB()
    runtime.observe_completed_call(CompletedCall("repo", "exec", "python job.py", 0, 1,
        cpu_time_seconds=2, cpu_time_eligible=True, cpu_peak_cores=4, cpu_peak_cores_eligible=True,
        cpu_peak_window_ms=500, sampled_peak_rss_bytes=64 * 1024**2,
        sampled_peak_rss_eligible=True, memory_total_peak_bytes=64 * 1024**2,
        memory_eligible=True, memory_measurement="cgroup_v2_environment_union_v1", memory_baseline_bytes=0, memory_extra_peak_bytes=64 * 1024**2, memory_environment_id="test"))
    result, diagnostics = predict_call_load(runtime=runtime, trie=trie, lattice=lattice,
        query=ToolCallQuery("repo", "exec", "python job.py", 10), edges=EDGES)
    assert all(v.backend == "runtime" for v in result.targets.values())
    root = Path(__file__).resolve().parents[3]
    schemas = [json.loads(p.read_text()) for p in (root / "contracts").glob("*.schema.json")]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)
    schema = next(s for s in schemas if s["$id"].endswith("/call-load.schema.json"))
    for prediction in [result, *diagnostics.backends.values()]:
        assert set(prediction.targets) == set(TARGET_UNITS)
        if prediction is result:
            assert all(t.status == "available" for t in prediction.targets.values())
            assert prediction.targets["cpu_avg_cores"].avg == 2
            assert prediction.targets["memory_total_peak_bytes"].p90 == 64 * 1024**2
        elif prediction.clause_predictions:
            assert "tool_hook_overhead_assumed_zero" in prediction.targets["duration_ms"].assumptions
            assert prediction.clause_predictions[0].targets["duration_ms"].p50 == 1000
            assert prediction.clause_predictions[0].targets["cpu_avg_cores"].avg == 2
        Draft202012Validator(schema, registry=registry).validate(prediction.model_dump())
        assert CallLoadPrediction.model_validate(prediction.model_dump()) == prediction
    from clawtune_sidecar.contracts.models import ToolPrediction
    public = ToolPrediction(tool=result, trie=diagnostics.backends["trie"],
                            lattice=diagnostics.backends["lattice"], call_prediction=result)
    decision_schema = next(s for s in schemas if s["$id"].endswith("/tool-decision.schema.json"))
    prediction_schema = dict(decision_schema["properties"]["prediction"], **{
        "$id": decision_schema["$id"], "$defs": decision_schema["$defs"],
    })
    Draft202012Validator(prediction_schema, registry=registry).validate(public.model_dump())


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
    assert "cpu_peak_cores" not in values and "memory_total_peak_bytes" not in values
    restored = RuntimeToolResourceKB.from_json_obj(kb.to_json_obj())
    assert restored.predict_load_samples(ToolCallQuery("r", "read", None, 2)) == values
    with pytest.raises(ValueError):
        restored.predict_load_samples(ToolCallQuery("r", "read", None, 1))


def test_v1_cpu_labels_quarantined():
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall("r", "read", None, 0, 1, cpu_peak_cores=2, cpu_peak_cores_eligible=True))
    data = kb.to_json_obj()
    data["schema"] = "runtime_tool_resource_kb_v1"
    with pytest.raises(ValueError, match="unsupported"):
        RuntimeToolResourceKB.from_json_obj(data)


def test_loop_samples_not_reused_as_standalone():
    rows = [row(in_loop=True)]
    for kb in (ClauseResourceKB.fit_public(rows), LatticeTimeKB.fit(rows)):
        assert kb.predict_load_samples("repo", [{"bin": "python", "argv": ["python", "job.py"]}], 3) == ({},)


def test_downstream_pipe_consumer_label_is_not_trained_as_standalone():
    clean = row(bin="grep", argv=("grep", "needle", "file"), latency_ms=100)
    polluted = row(
        i=2,
        bin="grep",
        argv=("grep", "needle"),
        latency_ms=90_000,
        in_pipe=True,
        pipeline_position=1,
    )
    query = [{"bin": "grep", "argv": ["grep", "needle", "file"]}]
    for kb in (ClauseResourceKB.fit_public([clean, polluted]), LatticeTimeKB.fit([clean, polluted])):
        values = kb.predict_load_samples("repo", query, 4)[0]["duration_ms"]["values"]
        assert set(values) == {100.0}


@pytest.mark.parametrize("head,consumer", [("head", None), ("cat", None), ("cat", "head"), ("python", "tail")])
def test_only_downstream_consumers_are_excluded_from_learning_and_prediction(head, consumer):
    argv = (head, "file.txt")
    first = dict(bin=head, argv=list(argv), in_pipe=consumer is not None,
                 pipeline_position=0 if consumer else -1)
    clauses = [first]
    observations = [row(bin=head, argv=argv, in_pipe=consumer is not None,
                        pipeline_position=0 if consumer else -1)]
    if consumer:
        clauses.append(dict(bin=consumer, argv=[consumer, "-20"], in_pipe=True, pipeline_position=1))
        observations.append(row(bin=consumer, argv=(consumer, "-20"), in_pipe=True,
                                pipeline_position=1, latency_ms=90000))
    _, diagnostics = predict_call_load(runtime=RuntimeToolResourceKB(),
        trie=ClauseResourceKB.fit_public(observations), lattice=LatticeTimeKB.fit(observations),
        query=ToolCallQuery("repo", "exec", " ".join(argv), 10), edges=EDGES,
        parsed_clauses=clauses)
    for name in ("trie", "lattice"):
        prediction = diagnostics.backends[name]
        assert prediction.targets["duration_ms"].p50 == 1000
        assert len(prediction.clause_predictions) == 1
        assert prediction.clause_predictions[0].argv == list(argv)


def test_pre_filter_aggregated_clause_snapshot_is_rejected():
    snapshot = ClauseResourceKB.fit_public([row()]).to_json_obj()
    snapshot["schema"] = "runtime_clause_resource_kb_v4"
    with pytest.raises(ValueError, match="unsupported clause schema"):
        ClauseResourceKB.from_json_obj(snapshot)


def test_resource_only_training_and_per_target_fault_isolation(monkeypatch):
    rows = [row(latency_ms=None, memory_total_peak_bytes=0, memory_extra_peak_bytes=0)]
    clause = [{"bin": "python", "argv": ["python", "job.py"]}]
    for kb in (ClauseResourceKB.fit_public(rows), LatticeTimeKB.fit(rows)):
        result = kb.predict_load_samples("repo", clause, 3)[0]
        assert "duration_ms" not in result and "cpu_avg_cores" not in result
        assert result["cpu_time_seconds"]["values"] == (2,)
        assert result["memory_total_peak_bytes"]["values"] == (0,)
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
    assert _predicted_cpu_millis({"continuous_predictions": {"cpu_peak_cores": {"conditional_p90": 100}}}) == 1000


def test_tool_kb_uses_retained_workload_duration_for_duration_and_cpu_average():
    from tool_resource.runtime_kb import _target_values
    values = _target_values(CompletedCall(
        "repo", "exec", "python job.py | tail -20", 0, 10,
        workload_duration_seconds=2,
        cpu_time_seconds=1,
        cpu_time_eligible=True,
    ))
    assert values["latency_ms"] == 10000
    assert values["workload_latency_ms"] == 2000
    assert values["cpu_time_seconds"] == 1
    assert values["cpu_avg_cores"] == .5
    missing = _target_values(CompletedCall(
        "repo", "exec", "python job.py", 0, 10,
        workload_duration_required=True,
        cpu_time_seconds=1,
        cpu_time_eligible=True,
    ))
    assert missing["latency_ms"] == 10000
    assert "workload_latency_ms" not in missing
    assert "cpu_avg_cores" not in missing
    assert missing["cpu_time_seconds"] == 1


def test_retained_workload_duration_unions_intervals_and_excludes_pipe_consumer():
    from clawtune_sidecar.predictors.tool_resource import _retained_workload_duration_seconds
    artifact = {"calls": [{
        "eligible_for_kb": True,
        "clauses": [
            {"bin": "python", "argv": ["python", "job.py"], "ts_start": 1.0,
             "ts_end": 3.0, "in_pipe": True, "pipeline_position": 0},
            {"bin": "tail", "argv": ["tail", "-20"], "ts_start": 1.1,
             "ts_end": 3.1, "in_pipe": True, "pipeline_position": 1},
            {"bin": "cat", "argv": ["cat", "result"], "ts_start": 4.0,
             "ts_end": 5.0, "in_pipe": False, "pipeline_position": -1},
        ],
    }]}
    assert _retained_workload_duration_seconds(artifact) == 3.0


def test_censored_resource_totals_not_used_as_complete_load():
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall("r", "read", None, 0, 1, censored=True,
        cpu_time_seconds=2, cpu_time_eligible=True, cpu_peak_cores=4, cpu_peak_cores_eligible=True,
        cpu_peak_window_ms=500, memory_total_peak_bytes=100, memory_eligible=True,
        memory_measurement="cgroup_v2_environment_union_v1", memory_baseline_bytes=0, memory_extra_peak_bytes=100, memory_environment_id="test", outcome="timeout"))
    assert kb.predict_load_samples(ToolCallQuery("r", "read", None, 2)) == {}


def test_call_level_evaluation_scores_and_metric_guard():
    from clawtune_sidecar.predictors.call_load_eval import evaluate_calls
    prediction = compose("trie", [{"duration_ms": {"values": [100, 200]}}], EDGES).model_dump()
    record = dict(prediction=prediction, scope="tool_call", lifecycle="tool_hook_interval",
                  actual={"duration_ms": {"valid": True, "value": 200, "metric_definition": "retained_workload_elapsed"}})
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


def test_models_never_fill_missing_tool_or_clause_targets_from_each_other():
    from types import SimpleNamespace
    runtime = SimpleNamespace(predict_load_samples=lambda query: {})
    trie = SimpleNamespace(predict_load_samples=lambda *args: ({"cpu_time_seconds": {"values": [2]}},))
    lattice = SimpleNamespace(predict_load_samples=lambda *args: ({"sampled_peak_rss_bytes": {"values": [64]}},))
    result, diagnostics = predict_call_load(runtime=runtime, trie=trie, lattice=lattice,
        query=ToolCallQuery("repo", "exec", "python job.py", 10), edges=EDGES)
    assert all(t.status == "unavailable" for t in result.targets.values())
    assert result.clause_predictions == []
    assert diagnostics.backends["trie"].targets["cpu_time_seconds"].p50 == 2
    assert diagnostics.backends["trie"].targets["sampled_peak_rss_bytes"].status == "unavailable"
    assert diagnostics.backends["lattice"].targets["cpu_time_seconds"].status == "unavailable"
    assert diagnostics.backends["lattice"].targets["sampled_peak_rss_bytes"].p50 == 64
    assert diagnostics.backends["trie"].clause_predictions[0].targets["sampled_peak_rss_bytes"].status == "unavailable"


def test_excluded_consumer_is_not_claimed_as_complete_tool_workload():
    from types import SimpleNamespace
    runtime = SimpleNamespace(predict_load_samples=lambda query: {})
    kb = SimpleNamespace(predict_load_samples=lambda *args: ({"cpu_time_seconds": {"values": [2]}},))
    _, diagnostics = predict_call_load(runtime=runtime, trie=kb, lattice=kb,
        query=ToolCallQuery("repo", "exec", "python job.py | cat", 10), edges=EDGES)
    for name in ("trie", "lattice"):
        target = diagnostics.backends[name].targets["cpu_time_seconds"]
        assert target.p50 == 2
        assert "listed_downstream_consumers_excluded" in target.assumptions
        assert "foreground_clause_lineage_covers_call_workload" not in target.assumptions
        assert diagnostics.backends[name].clause_predictions[0].targets["cpu_time_seconds"].p50 == 2
