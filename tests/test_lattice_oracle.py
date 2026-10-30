from types import SimpleNamespace

import pytest

from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import compose
from offline.lattice_oracle import LatticeOracle
from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB

EDGES = load_bucket_edges((100, 500, 2000, 10000))


def candidate(*values):
    return {"values": values, "context": ("oracle", str(values))}


def fake_kb(choices):
    return SimpleNamespace(_frozen=True, duration_candidate_samples=lambda *args: choices)


def test_oracle_optimizes_composed_call_not_each_clause_independently():
    kb = fake_kb(((candidate(1), candidate(9)), (candidate(1), candidate(9))))
    clauses = [{"argv": ["a"]}, {"argv": ["b"]}]
    evidence = LatticeOracle(kb, 10, EDGES).predict_load_samples("r", clauses, 1)
    assert [row["duration_ms"]["values"] for row in evidence] == [(1,), (9,)]
    result = compose("lattice", evidence, EDGES, clauses=clauses)
    assert result.targets["duration_ms"].p50 == 10
    assert result.targets["cpu_peak_cores"].unavailable_reason == "oracle_duration_only"


def test_oracle_retains_pipeline_composition():
    kb = fake_kb(((candidate(1), candidate(9)), (candidate(1), candidate(9))))
    clauses = [{"argv": ["a"], "in_pipe": True, "pipeline_position": 0},
               {"argv": ["b"], "in_pipe": True, "pipeline_position": 1}]
    evidence = LatticeOracle(kb, 9, EDGES).predict_load_samples("r", clauses, 1)
    assert [row["duration_ms"]["values"] for row in evidence] == [(1,), (9,)]
    assert compose("lattice", evidence, EDGES, clauses=clauses).targets["duration_ms"].p50 == 9


def test_oracle_deduplicates_only_identical_ordered_samples_and_preserves_ties(monkeypatch):
    import offline.lattice_oracle as module

    a = {"values": (1, 3), "context": ("oracle", "a")}
    duplicate = {"values": (1, 3), "context": ("oracle", "b")}
    reordered = {"values": (3, 1), "context": ("oracle", "c")}
    kb = fake_kb(((a, duplicate, reordered), (candidate(1),)))
    calls = []
    original = module.duration_p50

    def counted(*args, **kwargs):
        calls.append(tuple(args[0][0]))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "duration_p50", counted)
    evidence = LatticeOracle(kb, 10, EDGES).predict_load_samples("r", [{"argv": ["a"]}, {"argv": ["b"]}], 1)
    assert calls == [(1, 3), (3, 1)]
    assert evidence[0]["duration_ms"]["context"] == ("oracle", "a")


@pytest.mark.parametrize("actual", [None, True, float("nan"), float("inf"), -1])
def test_oracle_requires_valid_label_without_replacing_it_with_zero(actual):
    kb = fake_kb(((candidate(0),),))
    evidence = LatticeOracle(kb, actual, EDGES).predict_load_samples("r", [{"argv": ["a"]}], 1)
    assert evidence[0]["duration_ms"]["unavailable_reason"] == "oracle_requires_actual_duration"


def test_oracle_uses_candidate_median_and_keeps_full_distribution():
    kb = fake_kb(((candidate(0, 100, 100), candidate(1000, 70, 70)),))
    clauses = [{"argv": ["a"]}]
    evidence = LatticeOracle(kb, 80, EDGES).predict_load_samples("r", clauses, 1)
    assert evidence[0]["duration_ms"]["values"] == (1000, 70, 70)
    result = compose("lattice", evidence, EDGES, clauses=clauses).targets["duration_ms"]
    assert (result.p50, result.p90) == (70, 1000)
    assert result.buckets.probabilities == [2/3, 0, 1/3, 0, 0]
    assert "oracle_uses_actual_call_duration" in result.assumptions


def test_oracle_zero_is_a_valid_label_and_does_not_synthesize_samples():
    kb = fake_kb(((candidate(10), candidate(20)),))
    row = LatticeOracle(kb, 0, EDGES).predict_load_samples("r", [{"argv": ["a"]}], 1)[0]
    assert row["duration_ms"]["values"] == (10,)


def test_oracle_keeps_exact_own_samples_entry_constraints_and_frozen_state():
    def obs(argv, ms, i):
        return ClauseObservation("r", argv[0], argv, i, i+.1, latency_ms=ms)
    full = ("python", "-m", "pytest", "-q")
    rows = [obs(full, 10, 1), obs((*full, "tests"), 1000, 2),
            obs(("python", "other.py"), 500, 3)]
    kb = LatticeTimeKB.fit(rows)
    with pytest.raises(ValueError, match="frozen"):
        LatticeOracle(kb, 1000, EDGES)
    kb.freeze()
    before = kb.to_json_obj()
    oracle = LatticeOracle(kb, 1000, EDGES)
    result = oracle.predict_load_samples("r", [{"argv": full}], 100)
    assert result[0]["duration_ms"]["values"] == (10,)
    missing = oracle.predict_load_samples("r", [{"argv": ("python", "unseen.py")}], 100)
    assert missing[0]["duration_ms"]["unavailable_reason"] == "missing_clause_evidence"
    # Changing the test label never mutates training observations or advances a frozen KB.
    LatticeOracle(kb, 0, EDGES).predict_load_samples("r", [{"argv": full}], 100)
    assert kb.to_json_obj() == before


def test_oracle_has_no_evidence_when_one_retained_stage_is_missing():
    kb = fake_kb(((candidate(10),), ()))
    rows = LatticeOracle(kb, 10, EDGES).predict_load_samples("r", [{"argv": ["a"]}, {"argv": ["b"]}], 1)
    assert all(row["duration_ms"]["unavailable_reason"] == "missing_clause_evidence" for row in rows)
