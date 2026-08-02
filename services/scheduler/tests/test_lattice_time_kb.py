from __future__ import annotations

import copy
import json
import shlex
import statistics
from collections.abc import Iterable, Sequence
from typing import Any

import pytest

from tool_resource.runtime_kb import ClauseObservation
from tool_time._lattice_vendor.nodes import build_nodes
from tool_time._lattice_vendor.normalize import normalize_command
from tool_time._lattice_vendor.schemas import Observation
from tool_time._lattice_vendor.selector import predict as vendored_predict
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances
from tool_time.lattice_kb import (
    LATTICE_TIME_ALGORITHMS,
    LATTICE_TIME_KB_SCHEMA,
    ClauseLatticeTimePredictions,
    LatticeTimeKB,
    LatticeTimePrediction,
)


def _observation(
    repo: str,
    argv: Sequence[str],
    latency_ms: float | None,
    *,
    ts_start: float,
    ts_end: float | None = None,
    bin_: str | None = None,
) -> ClauseObservation:
    values = tuple(argv)
    return ClauseObservation(
        repo=repo,
        bin=bin_ or values[0].replace("\\", "/").rsplit("/", 1)[-1],
        argv=values,
        ts_start=ts_start,
        ts_end=ts_start + 0.5 if ts_end is None else ts_end,
        latency_ms=latency_ms,
    )


def _clause(argv: Sequence[str], *, bin_: str | None = None) -> dict[str, Any]:
    values = list(argv)
    return {
        "bin": bin_ or values[0].replace("\\", "/").rsplit("/", 1)[-1],
        "argv": values,
    }


def _only_clause(
    outcomes: tuple[ClauseLatticeTimePredictions, ...],
) -> ClauseLatticeTimePredictions:
    assert len(outcomes) == 1
    return outcomes[0]


def _by_algorithm(
    outcome: ClauseLatticeTimePredictions,
) -> dict[str, LatticeTimePrediction]:
    return {prediction.algorithm: prediction for prediction in outcome.predictions}


def _assert_unavailable(
    prediction: LatticeTimePrediction,
    reason: str,
) -> None:
    assert prediction.prediction_ms is None
    assert prediction.unavailable_reason == reason
    assert prediction.selected_features == ()
    assert prediction.evidence_count == 0
    assert prediction.selected_risk is None
    assert prediction.exact_match is None
    assert prediction.fallback is None


def _training_rows(
    observations: Iterable[ClauseObservation],
) -> list[Observation]:
    ordered = sorted(
        observations,
        key=lambda item: (
            item.ts_end,
            item.ts_start,
            item.repo,
            item.bin,
            item.argv,
        ),
    )
    return [
        Observation(
            cmd=shlex.join(item.argv),
            duration_s=float(item.latency_ms) / 1000.0,
            repo=item.repo,
            clause_index=0,
        )
        for item in ordered
        if item.latency_ms is not None
    ]


def test_three_algorithms_match_vendored_lattice_core() -> None:
    history = (
        _observation(
            "org/repo-a",
            ("python", "task.py", "--mode", "fast"),
            100.0,
            ts_start=1.0,
        ),
        _observation(
            "org/repo-a",
            ("python", "task.py", "--mode", "fast"),
            140.0,
            ts_start=2.0,
        ),
        _observation(
            "org/repo-a",
            ("python", "task.py", "--mode", "slow"),
            600.0,
            ts_start=3.0,
        ),
        _observation(
            "org/repo-b",
            ("python", "task.py", "--mode", "fast"),
            220.0,
            ts_start=4.0,
        ),
        _observation(
            "org/repo-b",
            ("python", "task.py", "--mode", "slow"),
            820.0,
            ts_start=5.0,
        ),
    )
    query_repo = "org/repo-a"
    query_argv = ("python", "task.py", "--mode", "fast", "--verbose")
    kb = LatticeTimeKB.fit(history)

    actual_clause = _only_clause(
        kb.predict_clauses(query_repo, [_clause(query_argv)], ts_start=20.0)
    )
    actual = _by_algorithm(actual_clause)
    assert tuple(prediction.algorithm for prediction in actual_clause.predictions) == (
        LATTICE_TIME_ALGORITHMS
    )
    assert set(actual) == set(LATTICE_TIME_ALGORITHMS)

    training = _training_rows(history)
    nodes, global_log_var, global_log_std = build_nodes(
        training,
        mode="bounded",
        max_optional_features=6,
        always_keep_exact=True,
        min_partial_support=1,
        estimator="median",
        split_compounds=False,
    )
    compute_shrinkage_variances(nodes, kappa=5.0, global_log_var=global_log_var)
    command = shlex.join(query_argv)

    for algorithm in ("shrinkage", "loso"):
        expected = vendored_predict(
            command,
            nodes,
            repo=query_repo,
            global_log_var=global_log_var,
            global_log_std=global_log_std,
            beta=0.0,
            gamma=0.0,
            delta=0.15,
            risk_method=algorithm,
            context_sample_alpha=0.03,
            estimator="median",
            shrinkage_kappa=5.0,
            loso_min_signatures=2,
            specificity_risk_tolerance=0.5,
            risk_weight=1.0,
        )
        prediction = actual[algorithm]
        assert prediction.prediction_ms == pytest.approx(expected.prediction_s * 1000.0)
        assert prediction.selected_features == tuple(expected.selected_features)
        assert prediction.evidence_count == expected.selected_sample_count
        assert prediction.selected_risk == pytest.approx(expected.selected_risk)
        assert prediction.exact_match is expected.exact_match
        assert prediction.fallback == (expected.fallback or None)
        assert prediction.unavailable_reason is None

    query_features, _ = normalize_command(command, repo=query_repo)
    best = None
    best_cardinality = -1
    for features, stats in nodes.items():
        if features.issubset(query_features) and len(features) > best_cardinality:
            best = stats
            best_cardinality = len(features)
    assert best is not None
    max_cardinality = actual["max_cardinality"]
    assert max_cardinality.prediction_ms == pytest.approx(best.median_s * 1000.0)
    assert max_cardinality.selected_features == tuple(sorted(best.features))
    assert max_cardinality.evidence_count == best.count
    assert max_cardinality.selected_risk is None
    assert max_cardinality.exact_match is (best.features == query_features)
    assert max_cardinality.fallback is None
    assert max_cardinality.unavailable_reason is None


def test_one_flat_kb_mixes_common_and_repo_specific_nodes() -> None:
    history = (
        _observation(
            "org/repo-a",
            ("python", "task.py", "--mode", "fast"),
            1_000.0,
            ts_start=1.0,
        ),
        _observation(
            "org/repo-b",
            ("python", "task.py", "--mode", "fast"),
            3_000.0,
            ts_start=2.0,
        ),
    )
    kb = LatticeTimeKB.fit(history)
    assert kb.node_count > 0

    common = frozenset({"tool=python", "target=task.py"})
    repo_a = common | {"repo=org/repo-a"}
    repo_b = common | {"repo=org/repo-b"}
    assert kb._nodes[common].durations == pytest.approx([1.0, 3.0])
    assert kb._nodes[repo_a].durations == pytest.approx([1.0])
    assert kb._nodes[repo_b].durations == pytest.approx([3.0])

    query_features, _ = normalize_command(
        "python task.py --mode unseen",
        repo="org/repo-a",
        is_clause=True,
    )
    activated = {features for features in kb._nodes if features.issubset(query_features)}
    assert common in activated
    assert repo_a in activated
    assert repo_b not in activated

    snapshot = kb.to_json_obj()
    assert "public" not in snapshot
    assert "repo" not in snapshot
    assert {row["repo"] for row in snapshot["observations"]} == {
        "org/repo-a",
        "org/repo-b",
    }


def test_predict_clauses_filters_noexec_builtins_and_preserves_clause_indexes() -> None:
    kb = LatticeTimeKB()
    clauses = [
        _clause(("cd", "/workspace")),
        _clause(("python", "task.py")),
        _clause(("export", "MODE=test")),
        _clause(("pytest", "tests", "-q")),
    ]

    shell_outcomes = kb.predict_clauses(
        "org/repo-a",
        clauses,
        ts_start=1.0,
        parse_failed=True,
    )
    assert [(item.clause_index, item.bin, item.argv) for item in shell_outcomes] == [
        (1, "python", ("python", "task.py")),
        (3, "pytest", ("pytest", "tests", "-q")),
    ]
    for outcome in shell_outcomes:
        assert tuple(item.algorithm for item in outcome.predictions) == (
            LATTICE_TIME_ALGORITHMS
        )
        for prediction in outcome.predictions:
            _assert_unavailable(prediction, "parse_failed")

    nonshell_outcomes = kb.predict_clauses(
        "org/repo-a",
        clauses,
        ts_start=1.0,
        parse_failed=True,
        shell_command=False,
    )
    assert [item.clause_index for item in nonshell_outcomes] == [0, 1, 2, 3]


def test_online_observations_are_absorbed_only_when_strictly_causal() -> None:
    kb = LatticeTimeKB()
    completed = _observation(
        "org/repo-a",
        ("python", "task.py"),
        250.0,
        ts_start=1.0,
        ts_end=2.0,
    )
    clause = _clause(completed.argv)

    assert kb.observe_completed_clause(completed) is True
    assert kb.observation_count == 0
    assert kb.pending_count == 1

    before_completion = _only_clause(
        kb.predict_clauses(completed.repo, [clause], ts_start=1.5)
    )
    at_completion = _only_clause(
        kb.predict_clauses(completed.repo, [clause], ts_start=2.0)
    )
    for outcome in (before_completion, at_completion):
        for prediction in outcome.predictions:
            _assert_unavailable(prediction, "no_lattice_time_evidence")
    assert kb.observation_count == 0
    assert kb.pending_count == 1

    causally_later = _only_clause(
        kb.predict_clauses(completed.repo, [clause], ts_start=2.000_001)
    )
    assert kb.observation_count == 1
    assert kb.pending_count == 0
    for prediction in causally_later.predictions:
        assert prediction.prediction_ms == pytest.approx(250.0)
        assert prediction.unavailable_reason is None
        assert prediction.evidence_count == 1

    with pytest.raises(ValueError, match="backdated lattice query"):
        kb.predict_clauses(completed.repo, [clause], ts_start=2.0)


def test_snapshot_round_trip_preserves_multiplicity_and_deduplicates_replay() -> None:
    historical = _observation(
        "org/repo-a",
        ("python", "task.py", "--mode", "fast"),
        100.0,
        ts_start=1.0,
        ts_end=2.0,
    )
    pending = _observation(
        "org/repo-b",
        ("python", "task.py", "--mode", "slow"),
        900.0,
        ts_start=3.0,
        ts_end=4.0,
    )
    kb = LatticeTimeKB.fit([historical, historical])
    assert kb.observation_count == 2
    assert kb.merge_historical([historical, historical]) == 0
    assert kb.merge_historical([historical, historical, historical]) == 1
    assert kb.observation_count == 3
    assert kb.observe_completed_clause(pending) is True
    assert kb.observe_completed_clause(pending) is True
    assert kb.pending_count == 2

    wire_snapshot = json.loads(json.dumps(kb.to_json_obj()))
    assert wire_snapshot["schema"] == LATTICE_TIME_KB_SCHEMA
    restored = LatticeTimeKB.from_json_obj(wire_snapshot)
    assert restored.observation_count == 3
    assert restored.pending_count == 2
    assert restored.merge_historical(
        [historical, historical, historical, pending, pending]
    ) == 0
    assert json.loads(json.dumps(restored.to_json_obj())) == wire_snapshot

    query = [_clause(("python", "task.py", "--mode", "unknown"))]
    expected = kb.predict_clauses("org/repo-a", query, ts_start=10.0)
    actual = restored.predict_clauses("org/repo-a", query, ts_start=10.0)
    assert actual == expected
    assert restored.observation_count == 5
    assert restored.pending_count == 0


def test_snapshot_rejects_bad_schema_generation_and_observation_shape() -> None:
    observation = _observation(
        "org/repo-a",
        ("python", "task.py"),
        100.0,
        ts_start=1.0,
    )
    snapshot = json.loads(json.dumps(LatticeTimeKB.fit([observation]).to_json_obj()))

    bad_schema = copy.deepcopy(snapshot)
    bad_schema["schema"] = "clause_lattice_time_kb_v999"
    with pytest.raises(ValueError, match="unsupported lattice KB schema"):
        LatticeTimeKB.from_json_obj(bad_schema)

    bad_generation = copy.deepcopy(snapshot)
    bad_generation["node_generation"]["max_optional_features"] = 5
    with pytest.raises(ValueError, match="node-generation configuration differs"):
        LatticeTimeKB.from_json_obj(bad_generation)

    bad_observation = copy.deepcopy(snapshot)
    bad_observation["observations"][0]["argv"] = ["python", 7]
    with pytest.raises(ValueError, match="observation argv must be non-empty strings"):
        LatticeTimeKB.from_json_obj(bad_observation)

    bad_timestamp = copy.deepcopy(snapshot)
    bad_timestamp["last_query_ts"] = float("nan")
    with pytest.raises(ValueError, match="last_query_ts must be finite or null"):
        LatticeTimeKB.from_json_obj(bad_timestamp)

    bad_latency = copy.deepcopy(snapshot)
    bad_latency["observations"][0]["latency_ms"] = None
    with pytest.raises(ValueError, match="latency_ms must be finite"):
        LatticeTimeKB.from_json_obj(bad_latency)


def test_empty_kb_returns_explicit_unavailability_for_every_algorithm() -> None:
    kb = LatticeTimeKB()
    assert kb.observation_count == 0
    assert kb.pending_count == 0
    assert kb.node_count == 0

    outcome = _only_clause(
        kb.predict_clauses(
            "org/repo-a",
            [_clause(("python", "task.py"))],
            ts_start=1.0,
        )
    )
    assert tuple(item.algorithm for item in outcome.predictions) == LATTICE_TIME_ALGORITHMS
    for prediction in outcome.predictions:
        _assert_unavailable(prediction, "no_lattice_time_evidence")


def test_max_cardinality_global_fallback_matches_flat_corpus_median() -> None:
    history = (
        _observation("org/a", ("python", "a.py"), 100.0, ts_start=1.0),
        _observation("org/b", ("pytest", "tests"), 900.0, ts_start=2.0),
        _observation("org/c", ("git", "status"), 500.0, ts_start=3.0),
    )
    kb = LatticeTimeKB.fit(history)
    outcome = _only_clause(
        kb.predict_clauses(
            "org/unseen",
            [_clause(("node", "script.js"))],
            ts_start=10.0,
        )
    )
    prediction = _by_algorithm(outcome)["max_cardinality"]
    assert prediction.prediction_ms == pytest.approx(
        statistics.median(item.latency_ms for item in history if item.latency_ms is not None)
    )
    assert prediction.selected_features == ()
    assert prediction.evidence_count == len(history)
    assert prediction.exact_match is False
    assert prediction.fallback == "global"
    assert prediction.unavailable_reason is None


def test_long_clause_bounds_node_generation_and_quadratic_shrinkage() -> None:
    training_argv = ("python", "task.py", *(f"--flag-{index}" for index in range(18)))
    query_argv = ("python", "task.py", "--unseen-flag", *training_argv[3:])
    kb = LatticeTimeKB.fit(
        [_observation("org/repo", training_argv, 500.0, ts_start=1.0)]
    )

    kb.prepare()

    assert kb.node_count <= 4_096
    outcome = _only_clause(
        kb.predict_clauses("org/repo", [_clause(query_argv)], ts_start=3.0)
    )
    shrinkage = _by_algorithm(outcome)["shrinkage"]
    _assert_unavailable(shrinkage, "lattice_candidate_limit_exceeded")
    assert _by_algorithm(outcome)["loso"].prediction_ms is not None
    assert _by_algorithm(outcome)["max_cardinality"].prediction_ms is not None
