from __future__ import annotations

import copy
import json

import pytest

from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LATTICE_TIME_ALGORITHMS, LatticeTimeKB
from tool_time.resource_lattice import RESOURCE_TARGETS, build_resource_states


def observation(index=0, **kwargs):
    values = dict(repo="org/repo", bin="python", argv=("python", "work.py"),
                  ts_start=float(index), ts_end=float(index + 1), latency_ms=1000.0,
                  cpu_ns_cumulative=2_000_000_000, peak_cpu_cores=3.0,
                  sampled_peak_rss_mb=512.0)
    values.update(kwargs)
    return ClauseObservation(**values)


def query(kb, ts=100, **kwargs):
    return kb.predict_resource_clauses("org/repo", [{"bin": "python", "argv": ["python", "work.py"]}], ts, **kwargs)[0]


def targets(result, algorithm="shrinkage"):
    return {row["target"]: row for row in result["predictions"] if row["algorithm"] == algorithm}


def test_quantiles_units_joint_cpu_average_and_thresholds():
    rows = [observation(i, cpu_ns_cumulative=i * 1_000_000_000, sampled_peak_rss_mb=i)
            for i in range(10)]
    kb = LatticeTimeKB.fit(rows)
    result = query(kb, thresholds={"cpu_avg_cores": 8.0})
    for algorithm in LATTICE_TIME_ALGORITHMS:
        output = targets(result, algorithm)
        assert set(output) == set(RESOURCE_TARGETS)
        for target in ("cpu_avg_cores", "cpu_time_seconds"):
            assert output[target]["p50"] == 4.5
            assert output[target]["p90"] == 8
            assert output[target]["evidence_count"] == 10
        assert output["cpu_avg_cores"]["probability_ge"] == 0.2
        assert output["memory_peak_rss_bytes"]["p50"] == 4.5 * 1024**2
        assert output["memory_peak_rss_bytes"]["p90"] == 8 * 1024**2
        assert output["cpu_peak_cores"]["p50"] == 3
    # Average is derived per observation, not ratio of independently estimated medians.
    other = LatticeTimeKB.fit([observation(0, latency_ms=100), observation(1, latency_ms=2000)])
    assert targets(query(other))["cpu_avg_cores"]["p50"] == 10.5


@pytest.mark.parametrize("latency", [None, 0.0])
def test_independent_eligibility_zero_and_missing_latency(latency):
    row = observation(latency_ms=latency, cpu_ns_cumulative=0, peak_cpu_cores=None, sampled_peak_rss_mb=0)
    kb = LatticeTimeKB.fit([row])
    output = targets(query(kb))
    assert output["cpu_time_seconds"]["p90"] == 0
    assert output["memory_peak_rss_bytes"]["p50"] == 0
    assert output["cpu_avg_cores"]["unavailable_reason"] == "no_lattice_resource_evidence"
    assert output["cpu_peak_cores"]["p50"] is None
    assert kb.predict_clauses("org/repo", [{"bin":"python", "argv":["python", "work.py"]}], 100)[0].predictions[0].prediction_ms is None
    restored = LatticeTimeKB.from_json_obj(json.loads(json.dumps(kb.to_json_obj())))
    assert query(restored) == query(kb)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_invalid_resource_does_not_pollute_other_targets(bad):
    kb = LatticeTimeKB.fit([observation(peak_cpu_cores=bad, cpu_ns_cumulative=None)])
    output = targets(query(kb))
    assert output["cpu_peak_cores"]["p50"] is None
    assert output["memory_peak_rss_bytes"]["p50"] == 512 * 1024**2
    snapshot = LatticeTimeKB.fit([observation()]).to_json_obj()
    snapshot["observations"][0]["peak_cpu_cores"] = bad
    with pytest.raises(ValueError, match="finite and non-negative"):
        LatticeTimeKB.from_json_obj(snapshot)


def test_causal_preparation_snapshot_and_duplicate_import():
    row = observation(1)
    kb = LatticeTimeKB()
    kb.observe_completed_clause(row)
    kb.prepare()
    assert targets(query(kb, ts=2))["cpu_time_seconds"]["p50"] is None
    snapshot = json.loads(json.dumps(kb.to_json_obj()))
    restored = LatticeTimeKB.from_json_obj(snapshot)
    assert restored.merge_historical([row]) == 0
    assert query(kb, ts=3) == query(restored, ts=3)
    assert targets(query(kb, ts=3))["cpu_time_seconds"]["p50"] == 2
    with pytest.raises(ValueError, match="backdated"):
        query(kb, ts=2)


def test_target_contexts_use_only_their_eligible_evidence():
    kb = LatticeTimeKB.fit([
        observation(cpu_ns_cumulative=None, peak_cpu_cores=None),
        observation(1, repo="org/other", sampled_peak_rss_mb=None),
    ])
    output = targets(query(kb))
    assert "repo=org/repo" in output["memory_peak_rss_bytes"]["selected_features"]
    assert "repo=org/repo" not in output["cpu_time_seconds"]["selected_features"]
    assert output["cpu_time_seconds"]["evidence_count"] == 1


def test_unknown_command_and_parse_failure_are_explicit():
    kb = LatticeTimeKB.fit([observation()])
    unknown = kb.predict_resource_clauses("org/repo", [{"bin":"git", "argv":["git", "status"]}], 100)[0]
    assert all(row["unavailable_reason"] == "no_matching_resource_context" for row in unknown["predictions"])
    assert all(row["unavailable_reason"] == "parse_failed" for row in query(kb, parse_failed=True)["predictions"])
    with pytest.raises(ValueError):
        query(kb, thresholds={"cpu_time_seconds": float("nan")})


def test_resource_positive_statistics_match_existing_lattice_numerics():
    from tool_time._lattice_vendor.nodes import build_nodes
    from tool_time._lattice_vendor.schemas import Observation
    rows = [observation(i, cpu_ns_cumulative=(i + 1) * 1_000_000_000) for i in range(4)]
    actual = build_resource_states(rows)["cpu_time_seconds"]
    expected, variance, std = build_nodes([
        Observation(cmd="python work.py", repo="org/repo", duration_s=i + 1)
        for i in range(4)
    ], mode="bounded", max_optional_features=6, estimator="median", split_compounds=False)
    assert set(actual.nodes) == set(expected)
    for fs in expected:
        for field in ("median_s", "std_log", "loo_mse_log", "loso_risk", "count"):
            assert getattr(actual.nodes[fs], field) == pytest.approx(getattr(expected[fs], field))
    assert actual.global_log_var == pytest.approx(variance)


def test_output_validates_public_schema():
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts/tool-decision.schema.json").read_text())
    validator = Draft202012Validator({"$ref":"#/$defs/clauseLatticeResourcePredictions", "$defs":schema["$defs"]})
    validator.validate(query(LatticeTimeKB.fit([observation()])))
    validator.validate(query(LatticeTimeKB()))


def test_legacy_snapshot_upgrade_and_no_fixed_threshold_storage():
    kb = LatticeTimeKB.fit([observation()])
    snapshot = kb.to_json_obj()
    legacy = copy.deepcopy(snapshot)
    legacy["schema"] = "clause_lattice_time_kb_v1"
    restored = LatticeTimeKB.from_json_obj(legacy)
    assert query(restored) == query(kb)
    before = restored.to_json_obj()
    query(restored, thresholds={"memory_peak_rss_bytes": 128 * 1024**2})
    assert restored.to_json_obj() == before
    assert "heavy" not in json.dumps(before)


def test_compound_predictions_keep_clause_scope_and_do_not_compose():
    kb = LatticeTimeKB.fit([observation(), observation(bin="git", argv=("git", "status"))])
    outcomes = kb.predict_resource_clauses("org/repo", [
        {"bin":"cd", "argv":["cd", "/workspace"]},
        {"bin":"python", "argv":["python", "work.py"]},
        {"bin":"git", "argv":["git", "status"]},
    ], 100)
    assert [row["clause_index"] for row in outcomes] == [1, 2]
    assert all(row["scope"] == "clause_owned_lineage" for row in outcomes)


def test_prepared_generation_contains_resources_without_query_rebuild(monkeypatch):
    kb = LatticeTimeKB()
    kb.observe_completed_clause(observation(1))
    kb.prepare()
    def unexpected_rebuild(*args, **kwargs):
        pytest.fail("resource query rebuilt an already prepared generation")
    monkeypatch.setattr("tool_time.lattice_kb._build_node_state", unexpected_rebuild)
    result = targets(query(kb, ts=3))
    assert result["memory_peak_rss_bytes"]["p50"] == 512 * 1024**2
    assert result["cpu_time_seconds"]["p50"] == 2
