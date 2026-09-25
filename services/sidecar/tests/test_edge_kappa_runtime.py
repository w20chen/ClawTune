from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from edge_kappa_kb import EdgeKappaKB, FeatureQuery, TimeOutcome, TrainingEvent
from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import compose, predict_call_load, summarize_weighted
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, RuntimeToolResourceKB, ToolCallQuery
from tool_time.lattice_kb import LatticeTimeKB

EDGES = load_bucket_edges((100, 500, 2000, 10000))


def row(index=0, ms=50, argv=("python", "job.py")):
    return ClauseObservation(repo="repo", bin=argv[0], argv=argv,
                             ts_start=index*10., ts_end=index*10.+1, latency_ms=ms)


def clauses(*commands):
    return [dict(argv=list(argv), bin=argv[0], clause_index=i, in_pipe=False,
                 pipeline_position=-1, in_loop=False, in_subst=False) for i, argv in enumerate(commands)]


def test_native_duration_weights_overlap_and_snapshot():
    core = frozenset({"tool=python"})
    def q(*extras):
        return FeatureQuery(core | set(extras), core, "python")
    training = [TrainingEvent(query, TimeOutcome(str(i), i*10, i*10+1, "task", str(i), "0", duration_ms=ms))
                for i, query, ms in [(1, q("a", "b", "z"), 50), (2, q("a", "b", "y"), 600)]]
    kb = EdgeKappaKB.fit(training, {"learn_weights": False, "max_optional_features": 1})
    # Both parents contain the same two events; their weights must sum per event.
    unseen = q("a", "b", "new")
    prediction = kb.predict_duration(unseen)
    assert len(prediction.bucket_prediction.evidence) == 2
    assert prediction.bucket_prediction.evidence_overlap_ratio == .5
    assert prediction.values_ms == (50, 600)
    assert sum(prediction.weights) == pytest.approx(1)
    assert prediction.evidence_count == 2
    saved = json.loads(json.dumps(kb.to_snapshot()))
    assert EdgeKappaKB.from_snapshot(saved).predict_duration(unseen) == prediction
    saved["completion_rows"]["1"]["duration_ms"] = 100000
    with pytest.raises(ValueError, match="duration"):
        EdgeKappaKB.from_snapshot(saved)


def test_censor_and_old_snapshots_never_fabricate_time_atoms():
    runtime = EdgeKappaRuntime.fit([row()], EDGES["duration_ms"])
    saved = runtime.to_snapshot()
    for observation in saved["completion_rows"].values():
        observation.pop("duration_ms")
    restored = EdgeKappaRuntime.from_snapshot(saved)
    evidence = restored.predict_load_samples("repo", clauses(("python", "job.py")), 10)
    assert evidence[0]["duration_ms"]["unavailable_reason"] == "non_exact_duration_evidence"
    assert compose("edge_kappa", evidence, EDGES).targets["duration_ms"].avg is None


def test_weighted_statistics_and_serial_pipeline_composition():
    estimate = summarize_weighted("duration_ms", EDGES["duration_ms"], "edge_kappa", [50, 600], [9, 1])
    assert estimate.avg == 105
    assert estimate.p50 == estimate.p90 == 50
    assert estimate.buckets.probabilities == [.9, 0, .1, 0, 0]
    evidence = [{"duration_ms": {"values": [100.], "weights": [1.]}} ,
                {"duration_ms": {"values": [600.], "weights": [3.]}}]
    serial = clauses(("python", "a.py"), ("python", "b.py"))
    assert compose("edge_kappa", evidence, EDGES, clauses=serial).targets["duration_ms"].avg == 700
    pipeline = [dict(c, in_pipe=True, pipeline_position=i) for i, c in enumerate(serial)]
    assert compose("edge_kappa", evidence, EDGES, clauses=pipeline).targets["duration_ms"].avg == 600
    stochastic = [{"duration_ms": {"values": [0., 100.], "weights": [9., 1.]}}] * 2
    result = compose("edge_kappa", stochastic, EDGES, clauses=serial)
    assert result == compose("edge_kappa", stochastic, EDGES, clauses=serial)
    assert result.targets["duration_ms"].avg == pytest.approx(20, abs=3)
    assert result.targets["duration_ms"].evidence_counts == [2, 2]


@pytest.mark.parametrize("count", [10, 12, 20])
def test_equal_parent_weights_preserve_available_predictions_and_quantiles(count):
    runtime = EdgeKappaRuntime.fit(
        [row(i, i + 1, ("python",)) for i in range(count)], EDGES["duration_ms"], frozen=True)
    _, diagnostics = predict_call_load(
        runtime=RuntimeToolResourceKB(), trie=ClauseResourceKB(), lattice=LatticeTimeKB(),
        edge_kappa=runtime, query=ToolCallQuery("repo", "exec", "python --verbose", 1000),
        edges=EDGES, parsed_clauses=clauses(("python", "--verbose")))
    result = diagnostics.backends["edge_kappa"]
    assert len(result.clause_predictions) == 1
    for estimate in (result.targets["duration_ms"],
                     result.clause_predictions[0].targets["duration_ms"]):
        assert estimate.status == "available"
        assert estimate.avg == pytest.approx((count + 1) / 2)
        assert estimate.p50 == (count + 1) / 2
        assert estimate.p90 == math.ceil(.9 * count)
        assert estimate.buckets.probabilities == [1., 0., 0., 0., 0.]
        assert estimate.evidence_counts == [count]


@pytest.mark.parametrize("scale", [1e-12, 1., 1e12])
def test_weighted_quantile_tolerance_does_not_hide_unequal_mass(scale):
    for delta, expected in ((-1e-12, 600), (0., 325), (1e-12, 50)):
        estimate = summarize_weighted("duration_ms", EDGES["duration_ms"], "edge_kappa",
                                      [50, 600], [(0.5 + delta) * scale, (0.5 - delta) * scale])
        assert estimate.p50 == expected
        assert estimate.p90 == 600
        assert math.fsum(estimate.buckets.probabilities) == pytest.approx(1.)


def test_fourth_backend_shared_contract_and_frozen_no_mutation():
    runtime = EdgeKappaRuntime.fit([row()], EDGES["duration_ms"], frozen=True)
    saved = runtime.to_snapshot()
    _, diagnostics = predict_call_load(runtime=RuntimeToolResourceKB(), trie=ClauseResourceKB(),
        lattice=LatticeTimeKB(), edge_kappa=runtime, query=ToolCallQuery("repo", "exec", "python job.py", 10),
        edges=EDGES, parsed_clauses=clauses(("python", "job.py")))
    result = diagnostics.backends["edge_kappa"]
    assert result.targets["duration_ms"].avg == 50
    assert result.clause_predictions[0].targets["duration_ms"].avg == 50
    assert result.targets["cpu_avg_cores"].unavailable_reason == "edge_kappa_time_only"
    root = Path(__file__).resolve().parents[3] / "contracts"
    documents = [json.loads(path.read_text()) for path in root.glob("*.schema.json")]
    registry = Registry().with_resources((doc["$id"], Resource.from_contents(doc)) for doc in documents)
    validator = Draft202012Validator(json.loads((root / "call-load.schema.json").read_text()), registry=registry)
    validator.validate(result.model_dump(mode="json"))
    Draft202012Validator(json.loads((root / "edge-kappa-kb.schema.json").read_text())).validate(saved)
    assert runtime.to_snapshot() == saved


def test_online_overlap_restart_retry_and_historical_deduplication():
    runtime = EdgeKappaRuntime.fit([row()], EDGES["duration_ms"])
    command = clauses(("python", "job.py"))
    before = runtime.predict_load_samples("repo", command, 10, call_id="long")
    runtime.predict_load_samples("repo", command, 11, call_id="short")
    new = replace(row(2, 600), ts_start=12, ts_end=13)
    runtime.complete_call("short", [new], commit_time=14)
    assert runtime.kb.generation == 2
    # Retrying a before request returns its original empirical evidence.
    assert runtime.predict_load_samples("repo", command, 15, call_id="long") == before
    runtime = EdgeKappaRuntime.from_snapshot(json.loads(json.dumps(runtime.to_snapshot())))
    assert runtime.predict_load_samples("repo", command, 15, call_id="long")[0]["duration_ms"]["values"] == list(before[0]["duration_ms"]["values"])
    long = replace(row(3, 2000), ts_start=10, ts_end=20)
    runtime.complete_call("long", [long], commit_time=20)
    assert runtime.kb.generation == 2  # strict end < query time
    runtime.predict_load_samples("repo", command, 21, call_id="next")
    assert runtime.kb.generation == 3
    runtime.complete_call("long", [long], commit_time=22)
    runtime.merge_historical([new, long, row()])
    assert runtime.kb.generation == 3
    runtime.complete_call("next", [], commit_time=23)
    assert not runtime.kb.to_snapshot()["tokens"]


def test_online_learns_weights_but_missing_tokens_only_add_counts():
    runtime = EdgeKappaRuntime.fit([row(0, 50, ("python",)), row(1, 600)], EDGES["duration_ms"])
    command = clauses(("python", "job.py"))
    runtime.predict_load_samples("repo", command, 20, call_id="observed")
    old = sum(edge.update_count for edge in runtime.kb.edges.values())
    runtime.complete_call("observed", [row(3, 50)], commit_time=40)
    assert sum(edge.update_count for edge in runtime.kb.edges.values()) > old
    old = sum(edge.update_count for edge in runtime.kb.edges.values())
    runtime.complete_call("no-token", [row(5, 600)], commit_time=60)
    assert sum(edge.update_count for edge in runtime.kb.edges.values()) == old


def test_dynamic_clauses_do_not_create_learning_tokens():
    runtime = EdgeKappaRuntime.fit([], EDGES["duration_ms"])
    command = [dict(clauses(("python", "job.py"))[0], prediction_unavailable_reason="dynamic_execution_structure")]
    assert runtime.predict_load_samples("repo", command, 10, call_id="dynamic")[0]["duration_ms"]["values"] == ()
    assert runtime.kb.to_snapshot()["tokens"] == {}


def test_fourth_snapshot_is_in_atomic_state_and_legacy_states_upgrade(tmp_path):
    from clawtune_kb import create_seed, initialize_state, StateStore
    from clawtune_kb.store import committed_state, write_json
    payloads = {"clause-resource-kb.json": ClauseResourceKB().to_json_obj(),
                "runtime-tool-resource-kb.json": RuntimeToolResourceKB().to_json_obj(),
                "clause-lattice-time-kb.json": LatticeTimeKB().to_json_obj()}
    seed, state = tmp_path / "seed", tmp_path / "state"
    create_seed(seed, payloads, provenance={})
    initialize_state(state, seed, owner="test")
    edge = EdgeKappaRuntime.fit([row()], EDGES["duration_ms"])
    with StateStore(state) as store:
        write_json(state / "edge-kappa-kb.json", edge.to_snapshot())
        store.checkpoint()
    committed = committed_state(state)
    assert "edge-kappa-kb.json" in committed["snapshots"]
    write_json(state / "edge-kappa-kb.json", {"corrupt": "uncommitted working file"})
    with StateStore(state):
        restored = EdgeKappaRuntime.from_snapshot(json.loads((state / "edge-kappa-kb.json").read_text()))
        assert restored.kb.generation == 1
    # A legacy commit must discard an uncommitted optional fourth file.
    legacy = tmp_path / "legacy"
    initialize_state(legacy, seed, owner="legacy")
    write_json(legacy / "edge-kappa-kb.json", edge.to_snapshot())
    with StateStore(legacy):
        assert not (legacy / "edge-kappa-kb.json").exists()


def test_true_censored_evidence_and_ambiguous_execution_do_not_invent_labels():
    runtime = EdgeKappaRuntime.fit([row()], EDGES["duration_ms"])
    query = next(iter(runtime.kb._observation_features.values()))
    feature_query = FeatureQuery(query, frozenset({"tool=python"}), "python")
    runtime.kb.complete("censored", TimeOutcome("censored", 10, 11, "t", "c", "0",
                        censor_lower_ms=10000), query=feature_query)
    runtime.kb.commit_before(12)
    result = runtime.kb.predict_duration(feature_query)
    assert result.unavailable_reason == "non_exact_duration_evidence"
    assert result.bucket_prediction.probabilities is not None
    assert not result.values_ms
    clean = EdgeKappaRuntime.fit([row(0, 50, ("python",)), row(1, 600)], EDGES["duration_ms"])
    repeated = clauses(("python", "job.py"), ("python", "job.py"))
    clean.predict_load_samples("repo", repeated, 20, call_id="repeated")
    old = sum(edge.update_count for edge in clean.kb.edges.values())
    clean.complete_call("repeated", [row(3, 50)], commit_time=40)
    assert sum(edge.update_count for edge in clean.kb.edges.values()) == old
    assert clean.kb.generation == 3
    assert not clean.kb.to_snapshot()["tokens"]


def test_parallel_feedback_replays_original_gradients_once_after_restart():
    baseline = EdgeKappaRuntime.fit([row(0, 50, ("python",)), row(1, 600)], EDGES["duration_ms"])
    tasks = []
    for name, index, duration in (("a", 3, 50), ("b", 4, 2000)):
        task = EdgeKappaRuntime.from_snapshot(baseline.to_snapshot())
        task.predict_load_samples("repo", clauses(("python", "job.py")), 20, call_id=name)
        task.complete_call(name, [row(index, duration)], commit_time=60)
        tasks.append(EdgeKappaRuntime.from_snapshot(json.loads(json.dumps(task.to_snapshot()))))
    merged = EdgeKappaRuntime.from_snapshot(baseline.to_snapshot())
    merged.merge_feedback(tasks[0])
    assert merged.kb.to_snapshot()["edges"] == tasks[0].kb.to_snapshot()["edges"]
    before = {key: edge.log_kappa for key, edge in merged.kb.edges.items()}
    merged.merge_feedback(tasks[1])
    for key, edge in merged.kb.edges.items():
        # Delayed gradients add to current log weights, not to source weights.
        delta = tasks[1].kb.edges[key].log_kappa - baseline.kb.edges[key].log_kappa
        assert edge.log_kappa == pytest.approx(before[key] + delta)
    assert merged.kb.generation == baseline.kb.generation + 2
    saved = merged.to_snapshot()
    merged = EdgeKappaRuntime.from_snapshot(json.loads(json.dumps(saved)))
    saved = merged.to_snapshot()
    schema_path = Path(__file__).resolve().parents[3] / "contracts/edge-kappa-kb.schema.json"
    Draft202012Validator(json.loads(schema_path.read_text())).validate(saved)
    for task in tasks:
        merged.merge_feedback(task)
    merged.merge_historical([row(3, 50), row(4, 2000)])
    assert merged.to_snapshot() == saved


def test_feedback_imports_new_nodes_and_keeps_count_only_completions():
    baseline = EdgeKappaRuntime.fit([row(0, 50, ("python",))], EDGES["duration_ms"])
    task = EdgeKappaRuntime.from_snapshot(baseline.to_snapshot())
    task.predict_load_samples("repo", clauses(("python", "job.py")), 20, call_id="new")
    task.complete_call("new", [row(3, 600)], commit_time=40)
    task.complete_call("no-token", [row(5, 2000, ("python", "other.py"))], commit_time=60)
    merged = EdgeKappaRuntime.from_snapshot(baseline.to_snapshot())
    merged.merge_feedback(task)
    assert merged.kb.to_snapshot()["edges"] == task.kb.to_snapshot()["edges"]
    assert merged.kb.to_snapshot()["observations"] == task.kb.to_snapshot()["observations"]
    restored = EdgeKappaRuntime.from_snapshot(merged.to_snapshot())
    assert restored.kb.generation == 3


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("legacy_tokens", [False, True])
def test_parallel_feedback_unions_different_historical_parents(reverse, shared, legacy_tokens):
    baseline = EdgeKappaRuntime.fit([row(0, 50, ("python",))], EDGES["duration_ms"])
    baseline.kb.shared_child_weight = shared
    tasks = [EdgeKappaRuntime.from_snapshot(baseline.to_snapshot()) for _ in range(2)]
    commands = [("python", "job.py"), ("python", "job.py", "--verbose")]
    observations = []
    for task, sequence, name in ((tasks[0], commands, "a"), (tasks[1], commands[1:], "b")):
        for argv in sequence:
            index = len(observations) + 1
            observation = row(index, 600 * index, argv)
            observations.append(observation)
            task.predict_load_samples("repo", clauses(argv), index * 10, call_id=f"{name}{index}")
            task.complete_call(f"{name}{index}", [observation], commit_time=index * 10 + 2)
        if legacy_tokens:
            for feedback in task._feedback.values():
                feedback["token"].pop("parent_sets")
    if reverse:
        tasks.reverse()

    # Each branch learned from different parent relations. Expected log weights
    # are the prior plus both branches' actual deltas, not copied final weights.
    expected = {key: edge.log_kappa for key, edge in baseline.kb.edges.items()}
    updates = {key: edge.update_count for key, edge in baseline.kb.edges.items()}
    for task in tasks:
        for key, edge in task.kb.edges.items():
            prior = (baseline.kb.edges[key].log_kappa if key in baseline.kb.edges
                     else math.log(task.kb.k0 / len(task.kb.graph.parents[key[1]])))
            expected.setdefault(key, prior)
            expected[key] += edge.log_kappa - prior
            updates[key] = updates.get(key, 0) + edge.update_count

    merged = EdgeKappaRuntime.from_snapshot(baseline.to_snapshot())
    for task in tasks:
        merged.merge_feedback(task)
        merged = EdgeKappaRuntime.from_snapshot(json.loads(json.dumps(merged.to_snapshot())))
    assert set(merged.kb.edges) == set(expected)
    for key, edge in merged.kb.edges.items():
        assert edge.log_kappa == pytest.approx(expected[key])
        assert edge.update_count == updates[key]
    assert merged.kb.generation == 4
    assert merged.predict_load_samples("repo", clauses(commands[-1]), 50)[0]["duration_ms"]["values"]
    saved = merged.to_snapshot()
    for task in tasks:
        merged.merge_feedback(task)
    merged.merge_historical(observations)
    assert merged.to_snapshot() == saved
    # Replaying an already merged snapshot must retain each original token's
    # parents, including after upgrading legacy feedback and restarting.
    replayed = EdgeKappaRuntime.from_snapshot(baseline.to_snapshot())
    replayed.merge_feedback(EdgeKappaRuntime.from_snapshot(json.loads(json.dumps(saved))))
    assert replayed.kb.generation == merged.kb.generation
    for key, edge in replayed.kb.edges.items():
        assert edge.log_kappa == pytest.approx(expected[key])
        assert edge.update_count == updates[key]


def test_legacy_learned_task_without_feedback_is_rejected():
    baseline = EdgeKappaRuntime.fit([row(0, 50, ("python",)), row(1, 600)], EDGES["duration_ms"])
    task = EdgeKappaRuntime.from_snapshot(baseline.to_snapshot())
    task.predict_load_samples("repo", clauses(("python", "job.py")), 20, call_id="legacy")
    task.complete_call("legacy", [row(3, 50)], commit_time=40)
    saved = task.to_snapshot()
    del saved["runtime"]["feedback"]
    with pytest.raises(ValueError, match="without replayable feedback"):
        baseline.merge_feedback(EdgeKappaRuntime.from_snapshot(saved))


@pytest.mark.parametrize("declared", [False, True])
def test_frozen_seed_manifest_distinguishes_missing_from_legacy_snapshot(tmp_path, declared):
    from clawtune_kb.store import digest, write_json
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    from tool_resource.runtime_kb import LatencyBuckets

    payloads = {"clause-resource-kb.json": ClauseResourceKB().to_json_obj(),
                "runtime-tool-resource-kb.json": RuntimeToolResourceKB().to_json_obj(),
                "clause-lattice-time-kb.json": LatticeTimeKB().to_json_obj()}
    for name, payload in payloads.items():
        write_json(tmp_path / name, payload)
    hashes = {name: digest(tmp_path / name) for name in payloads}
    if declared:
        hashes["edge-kappa-kb.json"] = "0" * 64
    write_json(tmp_path / "seed-manifest.json", {"snapshots": hashes})
    def load():
        return ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
            artifact_dir=tmp_path, buckets=LatencyBuckets(tuple(EDGES["duration_ms"])), frozen=True)
    if declared:
        with pytest.raises(ValueError, match="snapshot missing"):
            load()
    else:
        predictor = load()
        assert predictor.edge_kappa.kb.generation == 0
        predictor.close()
