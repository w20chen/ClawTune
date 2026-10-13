from dataclasses import replace

import pytest

from clawtune_sidecar.predictors.call_load import TARGET_UNITS, compose
from command_parser import CommandFeatureService, ParseContext
from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LATTICE_TIME_ALGORITHMS, LatticeTimeKB

FULL = ("python", "-m", "pytest", "-q")
PART = (*FULL, "tests/test_one.py")


def observation(argv, milliseconds, index):
    return ClauseObservation("r", argv[0], tuple(argv), float(index), index + .5,
                             latency_ms=milliseconds)


def clause(argv):
    return {"bin": argv[0], "argv": list(argv)}


def samples(kb, argv, algorithm="shrinkage", ts=100):
    return kb.predict_load_samples("r", [clause(argv)], ts, algorithm=algorithm)[0]


@pytest.mark.parametrize("argv", [FULL, ("python", "other.py")])
def test_entry_cannot_fall_back_to_generic_python(argv):
    kb = LatticeTimeKB.fit([observation(("python", "-c", "pass"), 1_000, 0)])
    for result in kb.predict_clauses("r", [clause(argv)], 100)[0].predictions:
        assert result.prediction_ms is None
        assert result.unavailable_reason == "no_matching_time_context"
    for algorithm in LATTICE_TIME_ALGORITHMS:
        assert "duration_ms" not in samples(kb, argv, algorithm)


def test_known_entry_remains_required_during_generalization():
    rows = [observation(("python", "-c", "pass"), 1, i) for i in range(10)]
    rows.append(observation(PART, 20_000, 11))
    kb = LatticeTimeKB.fit(rows)
    query = (*FULL, "tests/unseen.py")
    for result in kb.predict_clauses("r", [clause(query)], 100)[0].predictions:
        assert 'target:["pytest"]' in result.selected_features
        assert result.prediction_ms == 20_000


@pytest.mark.parametrize("own_values", [[80_000, 100_000], [80_000]])
def test_exact_own_samples_control_point_quantiles_and_composition(own_values):
    rows = [observation(FULL, value, i) for i, value in enumerate(own_values)]
    rows += [observation(PART, 1_000, i + 10) for i in range(8)]
    kb = LatticeTimeKB.fit(rows)
    kb.prepare()
    features = CommandFeatureService().argv(FULL, ParseContext(repo="r")).features
    node = kb._nodes[features]
    assert node.own_durations == [v / 1000 for v in own_values]
    assert len(node.durations) == len(rows)
    expected = sum(own_values) / len(own_values)
    for result in kb.predict_clauses("r", [clause(FULL)], 100)[0].predictions:
        assert result.prediction_ms == expected
        assert result.evidence_count == len(own_values)
        assert result.exact_match
    state = kb._resource_states["load:duration_ms"]
    for algorithm in LATTICE_TIME_ALGORITHMS:
        result = state.predict("r", FULL, "duration_ms", algorithm, threshold=50_000)
        assert (result.p50, result.p90) == (expected, max(own_values))
        assert result.probability_ge == 1
        evidence = samples(kb, FULL, algorithm)
        assert evidence["duration_ms"]["values"] == tuple(own_values)
        composed = compose("lattice", [evidence], {target: [100, 500, 2000, 10000] for target in TARGET_UNITS})
        assert composed.targets["duration_ms"].p50 == expected
    # Exact queries must not replace/mutate the coverage distribution.
    assert len(node.durations) == len(rows)
    assert len(state.nodes[features].durations) == len(rows)
    restored = LatticeTimeKB.from_json_obj(kb.to_json_obj())
    assert samples(restored, FULL)["duration_ms"]["values"] == tuple(own_values)


def test_generalized_query_keeps_coverage_even_if_selected_node_has_own_samples():
    rows = [observation(FULL, 90_000, 0)]
    rows += [observation(PART, 1_000, i + 1) for i in range(8)]
    kb = LatticeTimeKB.fit(rows)
    # -x has no exact training signature. Its fallback is allowed to use
    # the broader pytest history, including the targeted test observations.
    values = samples(kb, (*FULL, "-x"), "max_cardinality")["duration_ms"]["values"]
    assert sorted(values) == [1_000] * 8 + [90_000]


def test_coverage_only_node_and_causal_online_transition_to_own_samples():
    kb = LatticeTimeKB.fit([observation(PART, 1_000, 0)])
    assert samples(kb, FULL, ts=2)["duration_ms"]["values"] == (1_000,)
    kb.observe_completed_clause(observation(FULL, 80_000, 10))
    successor = kb.fork_for_update()
    successor.prepare()
    assert samples(successor, FULL, ts=10.5)["duration_ms"]["values"] == (1_000,)
    assert samples(successor, FULL, ts=11)["duration_ms"]["values"] == (80_000,)
    assert samples(kb, FULL, ts=3)["duration_ms"]["values"] == (1_000,)


def test_own_samples_are_target_specific_and_zero_is_not_missing():
    full = replace(observation(FULL, 0, 0), cpu_ns_cumulative=None, cpu_peak_cores=0)
    part = replace(observation(PART, 1_000, 1), cpu_ns_cumulative=2_000_000_000)
    evidence = samples(LatticeTimeKB.fit([full, part]), FULL)
    assert evidence["duration_ms"]["values"] == (0,)
    assert evidence["cpu_time_seconds"]["values"] == (2,)
