from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import jsonschema
import pytest

from edge_kappa_kb import EdgeKappaKB, FeatureQuery, TimeOutcome, TrainingEvent
from edge_kappa_kb.graph import node_id


def query(*extra: str) -> FeatureQuery:
    core = frozenset({"tool=python"})
    return FeatureQuery(core | set(extra), core, "python")


def event(i: int, features: FeatureQuery, ms: float, *, start: float | None = None,
          task: str = "task") -> TrainingEvent:
    start = float(i * 10 if start is None else start)
    return TrainingEvent(features, TimeOutcome(str(i), start, start + 1, task,
                                                f"call-{i}", "0", duration_ms=ms))


def test_cover_counts_and_parent_difference() -> None:
    a, b = query("a"), query("a", "b")
    kb = EdgeKappaKB.fit([event(1, a, 50), event(2, b, 600)])
    root = kb.nodes[node_id(frozenset({"tool=python"}))]
    child = kb.nodes[node_id(b.features)]
    assert root.counts == [1, 0, 1, 0, 0]
    assert child.counts == [0, 0, 1, 0, 0]
    prediction = kb.predict(b)
    assert prediction.exact_match
    assert prediction.observation_count == 1
    assert all(item.difference_count >= 0 for item in prediction.evidence)
    assert math.isclose(sum(prediction.probabilities), 1)


def test_unseen_query_no_mutation_and_no_evidence() -> None:
    kb = EdgeKappaKB.fit([event(1, query("a"), 50)])
    before = kb.to_snapshot()
    unknown = kb.predict(query("new"))
    assert unknown.probabilities is not None
    assert not unknown.exact_match
    assert kb.predict(FeatureQuery(frozenset({"tool=other"}),
                                   frozenset({"tool=other"}), "other")).unavailable_reason == \
           "no_edge_lattice_evidence"
    assert kb.to_snapshot() == before


def test_delayed_commit_idempotence_and_strict_visibility() -> None:
    q = query("a")
    kb = EdgeKappaKB.fit([event(1, query(), 50)])
    first, token = kb.begin(q, "new", 100)
    assert kb.begin(q, "new", 100)[0] == first
    outcome = TimeOutcome("new", 100, 200, "task", "call", "0", duration_ms=600)
    kb.complete(token, outcome)
    kb.complete(token, outcome)
    assert kb.commit_before(200).committed == 0
    assert kb.predict(q) == first
    assert kb.commit_before(201).committed == 1
    assert kb.commit_before(300).committed == 0
    assert kb.predict(q).observation_count == 1


def test_gradient_finite_difference_and_two_parent_learning() -> None:
    left, right, both = query("a"), query("b"), query("a", "b")
    kb = EdgeKappaKB.fit([event(1, left, 50), event(2, right, 600),
                          event(3, both, 600)])
    child = node_id(both.features)
    prediction = kb.predict(both)
    assert len(prediction.evidence) == 2
    selected = next(item for item in prediction.evidence if "a" in item.parent_features)
    key = (selected.parent_id, child)
    base = kb.edges[key].log_kappa
    h = 1e-5
    kb.edges[key].log_kappa = base + h
    plus = -math.log(kb.predict(both).probabilities[2])
    kb.edges[key].log_kappa = base - h
    minus = -math.log(kb.predict(both).probabilities[2])
    kb.edges[key].log_kappa = base
    analytic = selected.kappa / (prediction.observation_count + .01 +
        sum(item.kappa for item in prediction.evidence)) * (
        1 - selected.distribution[2] / prediction.probabilities[2])
    assert analytic == pytest.approx((plus - minus) / (2*h), abs=1e-8)
    old = {item.parent_id: kb.edges[(item.parent_id, child)].log_kappa
           for item in prediction.evidence}
    kb.begin(both, "feedback", 100)
    kb.complete("feedback", TimeOutcome("feedback", 100, 101, "task", "call", "0",
                                        duration_ms=600))
    kb.commit_before(102)
    favored = max(prediction.evidence, key=lambda item: item.distribution[2]).parent_id
    disfavored = min(prediction.evidence, key=lambda item: item.distribution[2]).parent_id
    assert kb.edges[(favored, child)].log_kappa > old[favored]
    assert kb.edges[(disfavored, child)].log_kappa < old[disfavored]


def test_snapshot_pending_fork_and_schema() -> None:
    kb = EdgeKappaKB.fit([event(1, query(), 50)])
    kb.begin(query("a"), "later", 100)
    kb.complete("later", TimeOutcome("later", 100, 200, "task", "call", "0",
                                      duration_ms=600))
    snapshot = kb.to_snapshot()
    schema = json.loads((Path(__file__).resolve().parents[3] /
        "contracts/edge-kappa-kb.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(snapshot, schema)
    restored = EdgeKappaKB.from_snapshot(json.loads(json.dumps(snapshot)))
    fork = kb.fork_for_update()
    for item in (kb, restored, fork):
        assert item.commit_before(200).committed == 0
        assert item.commit_before(201).committed == 1
    assert kb.to_snapshot() == restored.to_snapshot() == fork.to_snapshot()


def test_censor_and_untrusted_labels() -> None:
    kb = EdgeKappaKB.fit([event(1, query(), 50)])
    for index, kwargs in enumerate(({"censor_lower_ms": 15000},
                                    {"censor_lower_ms": 600},
                                    {"duration_ms": 200, "trusted": False}), 2):
        ident = str(index)
        kb.begin(query(), ident, index * 10)
        kb.complete(ident, TimeOutcome(ident, index * 10, index * 10 + 1,
                                      "task", ident, "0", **kwargs))
        kb.commit_before(index * 10 + 2)
    assert kb.predict(query()).observation_count == 2
    assert kb.predict(query()).probabilities[-1] > 0


def test_online_graph_keeps_old_edges_and_restores_new_node() -> None:
    old = query("a", "b")
    kb = EdgeKappaKB.fit([event(1, old, 50)])
    old_parents = dict(kb.graph.parents)
    middle = query("a")
    kb.begin(middle, "middle", 100)
    assert all(kb.graph.parents[key] == value for key, value in old_parents.items())
    restored = EdgeKappaKB.from_snapshot(json.loads(json.dumps(kb.to_snapshot())))
    assert restored.graph.parents == kb.graph.parents
    assert restored.begin(middle, "middle", 100)[0] == kb.begin(middle, "middle", 100)[0]


def test_online_new_subset_backfills_historical_coverage() -> None:
    old = query("a", "b")
    middle = query("a")
    kb = EdgeKappaKB.fit([event(1, old, 50)], {"max_optional_features": 0})
    assert not kb.predict(middle).exact_match
    kb.begin(middle, "middle", 100)
    state = kb.nodes[node_id(middle.features)]
    assert state.observations == {"1"}
    assert state.counts == [1, 0, 0, 0, 0]
    restored = EdgeKappaKB.from_snapshot(json.loads(json.dumps(kb.to_snapshot())))
    assert restored.predict(middle) == kb.predict(middle)


def test_online_new_subset_receives_earlier_inflight_completion() -> None:
    kb = EdgeKappaKB.fit([event(1, query(), 50)], {"max_optional_features": 0})
    broad = query("a", "b")
    subset = query("a")
    kb.begin(broad, "long", 10)
    kb.complete("long", TimeOutcome("long", 10, 30, "task", "long", "0",
                                    duration_ms=600))
    kb.begin(subset, "short", 20)
    kb.complete("short", TimeOutcome("short", 20, 21, "task", "short", "0",
                                     duration_ms=50))
    kb.commit_before(31)
    state = kb.nodes[node_id(subset.features)]
    assert state.observations == {"long", "short"}
    assert state.counts == [1, 0, 1, 0, 0]
    assert EdgeKappaKB.from_snapshot(json.loads(json.dumps(kb.to_snapshot()))).predict(subset) == \
           kb.predict(subset)


def test_exported_snapshot_does_not_share_mutable_model_state() -> None:
    kb = EdgeKappaKB.fit([event(1, query(), 50)])
    kb.begin(query(), "pending", 10)
    kb.complete("pending", TimeOutcome("pending", 10, 11, "task", "pending", "0",
                                        duration_ms=600))
    pending_row = kb._pending[0][3]
    snapshot = kb.to_snapshot()
    saved = copy.deepcopy(snapshot)
    kb.commit_before(12)
    assert snapshot == saved
    assert EdgeKappaKB.from_snapshot(json.loads(json.dumps(saved))).predict(query()).observation_count == 1
    snapshot["nodes"][0]["counts"][0] = 999
    snapshot["pending"][0][3]["duration_ms"] = 999
    assert kb.predict(query()).observation_count == 2
    assert pending_row["duration_ms"] == 600


def test_completion_retry_requires_same_payload() -> None:
    kb = EdgeKappaKB.fit([event(1, query(), 50)])
    kb.begin(query(), "two", 100)
    original = TimeOutcome("two", 100, 101, "task", "call", "0", duration_ms=50)
    kb.complete("two", original)
    with pytest.raises(ValueError, match="differs"):
        kb.complete("two", TimeOutcome("two", 100, 101, "task", "call", "0",
                                       duration_ms=600))


def test_missing_token_records_counts_without_learning_and_survives_snapshot() -> None:
    parent, child = query(), query("a")
    kb = EdgeKappaKB.fit([event(1, parent, 50), event(2, child, 600)])
    edge = (node_id(parent.features), node_id(child.features))
    initial_weight = kb.edges[edge].log_kappa
    outcome = TimeOutcome("unseen", 100, 101, "task", "call", "0", duration_ms=50)
    with pytest.raises(ValueError, match="query required"):
        kb.complete("unseen", outcome)
    kb.complete("unseen", outcome, query=child)
    kb.complete("unseen", outcome, query=child)
    with pytest.raises(ValueError, match="first query"):
        kb.complete("unseen", outcome, query=parent)
    snapshot = kb.to_snapshot()
    schema = json.loads((Path(__file__).resolve().parents[3] /
        "contracts/edge-kappa-kb.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(snapshot, schema)
    restored = EdgeKappaKB.from_snapshot(json.loads(json.dumps(snapshot)))
    for model in (kb, restored):
        assert model.predict(child).observation_count == 1
        report = model.commit_before(102)
        assert report.committed == 1
        assert report.weight_update_skipped == 1
        assert report.weight_updates == 0
        assert model.predict(child).observation_count == 2
        assert model.edges[edge].log_kappa == initial_weight
        model.complete("unseen", outcome, query=child)


def test_mismatched_token_uses_completion_query_only_for_counts() -> None:
    parent, child = query(), query("a")
    kb = EdgeKappaKB.fit([event(1, parent, 50), event(2, child, 600)])
    edge = (node_id(parent.features), node_id(child.features))
    initial_weight = kb.edges[edge].log_kappa
    kb.begin(child, "mismatch", 100, task_id="task", call_id="old", clause_id="0")
    outcome = TimeOutcome("mismatch", 100, 101, "task", "new", "0", duration_ms=50)
    kb.complete("mismatch", outcome, query=child)
    with pytest.raises(ValueError, match="already completed"):
        kb.begin(child, "mismatch", 100)
    report = kb.commit_before(102)
    assert report.weight_update_skipped == 1
    assert kb.predict(child).observation_count == 2
    assert kb.edges[edge].log_kappa == initial_weight


def test_missing_token_can_add_exact_node_and_backfill_coverage() -> None:
    broad, subset = query("a", "b"), query("a")
    kb = EdgeKappaKB.fit([event(1, broad, 50)], {"max_optional_features": 0})
    assert not kb.predict(subset).exact_match
    kb.complete("unseen", TimeOutcome("unseen", 100, 101, "task", "call", "0",
                                      duration_ms=600), query=subset)
    state = kb.nodes[node_id(subset.features)]
    assert state.observations == {"1"}
    assert kb.commit_before(102).weight_update_skipped == 1
    assert state.observations == {"1", "unseen"}
    assert state.counts == [1, 0, 1, 0, 0]


def test_projection_preserves_lower_bound_and_total_cap() -> None:
    from edge_kappa_kb.kb import _project
    projected, hit = _project([120, 90, 1e-10], 1e-6 / 3, 100)
    assert hit
    assert all(value >= 1e-6 / 3 for value in projected)
    assert sum(projected) == pytest.approx(100)


def test_equal_timestamp_cannot_see_completion() -> None:
    kb = EdgeKappaKB.fit([event(1, query(), 50)])
    before = kb.predict(query())
    kb.begin(query(), "one", 100)
    kb.complete("one", TimeOutcome("one", 100, 101, "task", "call", "0",
                                    duration_ms=600))
    prediction, _ = kb.begin(query(), "two", 101)
    assert prediction == before
    prediction, _ = kb.begin(query(), "three", 102)
    assert prediction != before


def test_shared_child_ablation_keeps_parent_weights_tied() -> None:
    left, right, both = query("a"), query("b"), query("a", "b")
    kb = EdgeKappaKB.fit([event(1, left, 50), event(2, right, 600),
                          event(3, both, 600)], {"shared_child_weight": True})
    child = node_id(both.features)
    kb.begin(both, "feedback", 100)
    kb.complete("feedback", TimeOutcome("feedback", 100, 101, "task", "call", "0",
                                        duration_ms=600))
    kb.commit_before(102)
    assert len({kb.edges[(parent, child)].log_kappa for parent in kb.graph.parents[child]}) == 1


def test_identical_parent_distributions_do_not_create_ranking() -> None:
    left, right, both = query("a"), query("b"), query("a", "b")
    kb = EdgeKappaKB.fit([event(1, left, 50), event(2, right, 50),
                          event(3, both, 600)])
    child = node_id(both.features)
    kb.begin(both, "feedback", 100)
    kb.complete("feedback", TimeOutcome("feedback", 100, 101, "task", "call", "0",
                                        duration_ms=600))
    kb.commit_before(102)
    weights = [kb.edges[(parent, child)].log_kappa for parent in kb.graph.parents[child]]
    assert weights[0] == pytest.approx(weights[1])


def test_many_parents_keep_initial_total_strength() -> None:
    features = tuple(f"feature-{index}" for index in range(8))
    training = [event(index + 1, query(feature), 50)
                for index, feature in enumerate(features)]
    training.append(event(20, query(*features), 600))
    kb = EdgeKappaKB.fit(training, {"max_optional_features": 1, "learn_weights": False})
    child = node_id(query(*features).features)
    parents = kb.graph.parents[child]
    assert len(parents) == 8
    assert sum(math.exp(kb.edges[(parent, child)].log_kappa) for parent in parents) == \
           pytest.approx(1.0)
