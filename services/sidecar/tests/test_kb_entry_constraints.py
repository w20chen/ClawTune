"""Execution identity across prefix backoff and learned Edge sharing."""
import copy
import json
from dataclasses import asdict, replace
from pathlib import Path

import jsonschema
import pytest

from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from command_parser import CommandFeatureService, ParseContext
from edge_kappa_kb import EdgeKappaKB, FeatureQuery, TimeOutcome, TrainingEvent
from edge_kappa_kb.graph import node_id
from tool_resource.runtime_kb import (
    ClauseObservation,
    ClauseResourceKB,
    CompletedCall,
    LatencyBuckets,
    RuntimeToolResourceKB,
    ToolCallQuery,
)
from tool_time.edge_kappa_adapter import feature_query


def clause(argv, index=0, ms=1000, cpu=2):
    return ClauseObservation("r", argv[0], tuple(argv), index * 100, index * 100 + 1,
                             latency_ms=ms, cpu_ns_cumulative=cpu * 1_000_000_000,
                             cpu_peak_cores=cpu)


def call(command, index=0, seconds=1, cpu=2, tool="exec"):
    return CompletedCall("r", tool, command, index * 100, index * 100 + seconds,
                         cpu_time_seconds=cpu, cpu_time_eligible=True,
                         cpu_peak_cores=cpu, cpu_peak_cores_eligible=True,
                         cpu_peak_window_ms=500)


def query_clause(argv):
    return {"bin": argv[0], "argv": list(argv)}


@pytest.mark.parametrize("argv", [
    ("python", "-m", "pytest", "-q"),
    ("python", "other.py"),
    ("python", "-B", "-u", "-m", "pytest", "-q"),
    ("git", "status"),
])
@pytest.mark.parametrize("public", [False, True])
def test_prefix_kbs_reject_unsafe_prefix_while_tool_uses_public_fallback(argv, public):
    history = (argv[0], "-c", "pass") if argv[0] == "python" else ("git", "diff")
    obs, completed = clause(history), call(" ".join(history))
    if public:
        trie, tool = ClauseResourceKB.fit_public([obs]), RuntimeToolResourceKB.fit_public([completed])
    else:
        trie, tool = ClauseResourceKB(), RuntimeToolResourceKB()
        trie.observe_completed_clause(obs)
        tool.observe_completed_call(completed)
    q = ToolCallQuery("r", "exec", " ".join(argv), 1000)
    evidence = tool.predict_load_samples(q)
    predictions = tool.query(q)
    if public:
        assert evidence["duration_ms"] == {
            "values": (1000,), "context": ("public", "tool_name"),
        }
        assert predictions["latency_ms"].scope == "public"
        assert predictions["latency_ms"].key_kind == "tool_name"
    else:
        assert evidence == {}
        assert all(result.conditional_p90 is None for result in predictions.values())
    trie_load = trie.predict_load_samples("r", [query_clause(argv)], 1000)
    if public:
        assert trie_load[0]["duration_ms"] == {
            "values": (1000.0,), "context": ("public", "global"),
        }
        assert trie.predict_clause_latency_bucket(
            "r", argv[0], argv, LatencyBuckets((100, 2000)),
        ).key_kind == "global"
    else:
        assert trie_load == ({},)
        with pytest.raises(ValueError):
            trie.predict_clause_latency_bucket(
                "r", argv[0], argv, LatencyBuckets((100, 2000)),
            )


def test_prefix_kbs_keep_exact_and_same_entry_generalization_after_restore():
    full = ("python", "-m", "pytest")
    part = (*full, "tests/a.py")
    trie, tool = ClauseResourceKB(), RuntimeToolResourceKB()
    for i, (argv, ms) in enumerate(((full, 80000), (part, 1000))):
        trie.observe_completed_clause(clause(argv, i, ms))
        tool.observe_completed_call(call(" ".join(argv), i, ms / 1000))
    assert trie.predict_load_samples("r", [query_clause(full)], 1000)[0]["duration_ms"]["values"] == (80000,)
    assert tool.predict_load_samples(ToolCallQuery("r", "exec", " ".join(full), 1000))["duration_ms"]["values"] == (80000,)
    trie = ClauseResourceKB.from_json_obj(trie.to_json_obj())
    tool = RuntimeToolResourceKB.from_json_obj(tool.to_json_obj())
    unseen = (*full, "tests/b.py")
    assert trie.predict_load_samples("r", [query_clause(unseen)], 1001)[0]["duration_ms"]["values"] == (80000, 1000)
    assert tool.predict_load_samples(ToolCallQuery("r", "exec", " ".join(unseen), 1001))["duration_ms"]["values"] == (80000, 1000)


@pytest.mark.parametrize("wrapper", [
    ("env", "X=1"), ("timeout", "30"), ("env", "X=1", "timeout", "30"),
])
@pytest.mark.parametrize("restore", [False, True])
def test_trie_wrapped_entry_uses_the_normalized_prefix_for_all_prediction_paths(wrapper, restore):
    service = CommandFeatureService()
    context = ParseContext(repo="r")
    base = ("python", "-m", "pytest")
    history = (*base, "tests/a.py")
    kb = ClauseResourceKB()
    kb.observe_completed_clause(replace(
        clause(history),
        command_features=service.argv((*wrapper, *history), context).to_dict(),
    ))
    # Check restoration of pending observations too, before causal absorption.
    if restore:
        kb = ClauseResourceKB.from_json_obj(kb.to_json_obj())
    argv = (*base, "tests/b.py")
    prepared = service.prepare([
        {**query_clause(argv), "syntax_argv": [*wrapper, *argv]},
    ], context)
    evidence = kb.predict_load_samples("r", prepared, 1000)[0]
    assert evidence["duration_ms"] == {
        "values": (1000,), "context": ("repo", "argv_prefix_depth_3"),
    }
    assert evidence["cpu_time_seconds"]["values"] == (2,)
    assert evidence["cpu_peak_cores"]["values"] == (2,)
    buckets = LatencyBuckets((100, 2000))
    single = kb.predict_command_latency_bucket_from_clauses("r", prepared, 1000, buckets)
    assert single.prediction.key_kind == "argv_prefix_depth_3"
    assert single.prediction.bucket_id == 1
    combined = kb.predict_command_latency_bucket_from_clauses(
        "r", [prepared[0], prepared[0]], 1000, buckets,
    )
    assert combined.composed_total_ms == 2000
    # A different module must still not fall back to python or python -m.
    other = ("python", "-m", "unittest", "tests/b.py")
    missing = service.prepare([
        {**query_clause(other), "syntax_argv": [*wrapper, *other]},
    ], context)
    assert kb.predict_load_samples("r", missing, 1000) == ({},)
    assert kb.predict_command_latency_bucket_from_clauses(
        "r", missing, 1000, buckets,
    ).prediction is None


@pytest.mark.parametrize("restore", [False, True])
def test_trie_contextual_script_identity_preserves_same_script_backoff(restore):
    service = CommandFeatureService()
    context = ParseContext(repo="r", workspace="/work/repo", cwd="/work/repo/scripts",
                           container_id="container")
    kb = ClauseResourceKB()
    history = ("python", "task.py", "old")
    kb.observe_completed_clause(replace(
        clause(history), command_features=service.argv(history, context).to_dict(),
    ))
    prepared = service.prepare([query_clause(("python", "task.py", "new"))], context)
    assert 'target:["scripts/task.py"]' in prepared[0]["command_features"]["core"]
    if restore:
        kb = ClauseResourceKB.from_json_obj(kb.to_json_obj())
    evidence = kb.predict_load_samples("r", prepared, 1000)[0]
    assert evidence["duration_ms"] == {
        "values": (1000,), "context": ("repo", "argv_prefix_depth_2"),
    }
    result = kb.predict_command_latency_bucket_from_clauses(
        "r", prepared, 1000, LatencyBuckets((100, 2000)),
    )
    assert result.prediction.key_kind == "argv_prefix_depth_2"
    other = service.prepare([query_clause(("python", "other.py", "new"))], context)
    assert kb.predict_load_samples("r", other, 1000) == ({},)


def test_tool_compound_prefix_fallback_exact_preference_and_commandless_history():
    tool = RuntimeToolResourceKB.fit_public([call("python a.py")])
    compound = "python a.py && python b.py"
    assert tool.predict_load_samples(ToolCallQuery("r", "exec", compound, 10))["duration_ms"] == {
        "values": (1000,), "context": ("public", "tool_name"),
    }
    tool.observe_completed_call(call("python a.py"))
    fallback = tool.predict_load_samples(ToolCallQuery("r", "exec", compound, 1000))["duration_ms"]
    assert fallback == {"values": (1000,), "context": ("repo", "command_prefix_depth_2")}
    tool.observe_completed_call(call(compound, 11, seconds=3))
    assert tool.predict_load_samples(ToolCallQuery("r", "exec", compound, 1200))["duration_ms"]["values"] == (3000,)
    restored = RuntimeToolResourceKB.from_json_obj(tool.to_json_obj())
    fallback = restored.predict_load_samples(ToolCallQuery("r", "exec", "python a.py && python c.py", 1200))["duration_ms"]
    assert fallback == {"values": (3000,), "context": ("repo", "command_prefix_depth_4")}
    tool.observe_completed_call(call(None, 13, seconds=2, tool="read"))
    assert tool.predict_load_samples(ToolCallQuery("r", "read", None, 1400))["duration_ms"]["values"] == (2000,)


def test_tool_public_global_is_the_final_fallback_for_an_unseen_tool_name():
    tool = RuntimeToolResourceKB.fit_public([call("python a.py")])
    query = ToolCallQuery("unseen/repo", "terminal_exec", "ruby new.rb", 10)

    load = tool.predict_load_samples(query)
    pmu = tool.predict_pmu_samples(query)

    assert load["duration_ms"] == {
        "values": (1000,), "context": ("public", "global"),
    }
    # Missing PMU targets train explicit imputed zeros at the same global node.
    assert len(pmu) == 9
    assert all(row == {"values": (0,), "context": ("public", "global")} for row in pmu.values())


def test_public_semantic_fallback_matches_entry_without_mixing_subcommands():
    pytest_obs = clause(("python3", "-m", "pytest", "tests/a.py"), ms=1000)
    diff_obs = replace(clause(("git", "diff", "a.py"), 1, ms=9000), repo="other")
    trie = ClauseResourceKB.fit_public([pytest_obs, diff_obs])
    tool = RuntimeToolResourceKB.fit_public([
        call("python3 -m pytest tests/a.py", seconds=1),
        replace(call("git diff a.py", 1, seconds=9), repo="other"),
    ])

    pytest_query = ("python", "-m", "pytest", "tests/b.py")
    trie_evidence = trie.predict_load_samples(
        "unseen", [query_clause(pytest_query)], 1000,
    )[0]["duration_ms"]
    tool_evidence = tool.predict_load_samples(
        ToolCallQuery("unseen", "exec", "python -m pytest tests/b.py", 1000),
    )["duration_ms"]
    assert trie_evidence == {
        "values": (1000.0,), "context": ("public", "semantic_core"),
    }
    assert tool_evidence == {
        "values": (1000,), "context": ("public", "semantic_call"),
    }

    # A different git subcommand cannot borrow the git-diff semantic node.
    status = ("git", "status")
    assert trie.predict_load_samples(
        "unseen", [query_clause(status)], 1001,
    )[0]["duration_ms"]["context"] == ("public", "global")
    assert tool.predict_load_samples(
        ToolCallQuery("unseen", "exec", "git status", 1001),
    )["duration_ms"]["context"] == ("public", "tool_name")


def test_tool_compound_call_never_uses_a_single_clause_semantic_node():
    tool = RuntimeToolResourceKB.fit_public([
        call("python3 -m pytest tests/a.py", seconds=1),
    ])

    evidence = tool.predict_load_samples(ToolCallQuery(
        "unseen", "exec", "python -m pytest tests/b.py && git status", 1000,
    ))

    assert evidence["duration_ms"]["context"] == ("public", "tool_name")


def test_tool_semantic_fallback_retains_the_complete_compound_structure():
    prefix = "cd -- /testbed || exit $?\n"
    tool = RuntimeToolResourceKB.fit_public([
        call(prefix + "python3 -m pytest tests/a.py && git diff a.py", seconds=7),
    ])

    evidence = tool.predict_load_samples(ToolCallQuery(
        "unseen",
        "exec",
        prefix + "python -m pytest tests/b.py && git diff b.py",
        1000,
    ))

    assert evidence["duration_ms"] == {
        "values": (7000,), "context": ("public", "semantic_call"),
    }


@pytest.mark.parametrize("separator", ["&&", ";", "|", "||"])
def test_tool_prefix_samples_represent_whole_calls_across_command_structures(separator):
    tool = RuntimeToolResourceKB()
    tool.observe_completed_call(call(f"python a.py {separator} python b.py", seconds=80))
    query = ToolCallQuery("r", "exec", f"python a.py {separator} python c.py", 1000)
    evidence = tool.predict_load_samples(query)
    assert evidence["duration_ms"] == {"values": (80000,), "context": ("repo", "command_prefix_depth_4")}
    assert evidence["cpu_time_seconds"]["values"] == (2,)
    legacy = tool.query(query)["latency_ms"]
    assert legacy.conditional_p90 == 80000
    assert legacy.key_kind == "command_prefix_depth_4"
    # The shorter prefix is also a complete-call distribution, not a clause label.
    shorter = tool.predict_load_samples(ToolCallQuery("r", "exec", "python a.py --new", 1001))
    assert shorter["duration_ms"] == {"values": (80000,), "context": ("repo", "command_prefix_depth_2")}


def test_exact_sample_selection_is_per_target_and_zero_remains_valid():
    full = ("python", "-m", "pytest")
    trie, tool = ClauseResourceKB(), RuntimeToolResourceKB()
    trie.observe_completed_clause(replace(clause(full, ms=80000), cpu_ns_cumulative=None, cpu_peak_cores=0))
    trie.observe_completed_clause(clause((*full, "tests/a.py"), 1))
    tool.observe_completed_call(replace(call(" ".join(full), seconds=80), cpu_time_seconds=None, cpu_peak_cores=0))
    tool.observe_completed_call(call(" ".join((*full, "tests/a.py")), 1))
    evidence = [trie.predict_load_samples("r", [query_clause(full)], 1000)[0],
                tool.predict_load_samples(ToolCallQuery("r", "exec", " ".join(full), 1000))]
    # Raw clause fragments retain None until ingress coalesces and imputes;
    # complete call records impute at their target boundary.
    for row, expected_cpu in zip(evidence, (2, 0)):
        assert row["duration_ms"]["values"] == (80000,)
        assert row["cpu_peak_cores"]["values"] == (0,)
        assert row["cpu_time_seconds"]["values"] == (expected_cpu,)


def edge_query(*optional, entry="pytest"):
    core = frozenset({"tool=python"} | ({f'target:["{entry}"]'} if entry else set()))
    return FeatureQuery(core | set(optional), core, "python")


def event(i, query, ms):
    return TrainingEvent(query, TimeOutcome(str(i), i * 10, i * 10 + 1,
                                           "task", str(i), "0", duration_ms=ms))


def test_edge_unseen_and_exact_nodes_cannot_borrow_across_entry():
    generic, full = edge_query(entry=None), edge_query("a")
    kb = EdgeKappaKB.fit([event(1, generic, 50)])
    assert kb.predict_duration(full).unavailable_reason == "no_edge_lattice_evidence"
    kb.begin(full, "new", 100)
    assert kb.predict_duration(full).unavailable_reason == "no_edge_lattice_evidence"
    kb.complete("new", TimeOutcome("new", 100, 101, "task", "new", "0", duration_ms=600))
    kb.commit_before(102)
    result = kb.predict_duration(full)
    assert result.values_ms == (600,)
    assert not result.bucket_prediction.evidence


@pytest.mark.parametrize("shared", [False, True])
def test_edge_learning_and_replay_keep_valid_borrowing_and_reject_invalid_parents(shared):
    # Two incomparable parents: one retains the entry, one retains only a flag.
    generic, entry, full = edge_query("a", entry=None), edge_query(), edge_query("a")
    kb = EdgeKappaKB.fit([event(1, generic, 50), event(2, entry, 50), event(3, full, 600)],
                         {"shared_child_weight": shared})
    baseline = kb.to_snapshot()
    child = node_id(full.features)
    forbidden = (node_id(generic.features), child)
    allowed = (node_id(entry.features), child)
    before_bad = copy.deepcopy(kb.edges[forbidden])
    before_good = kb.edges[allowed].update_count
    kb.begin(full, "feedback", 100)
    # Local predictions retain optional-feature borrowing during training.
    local = kb._tokens["feedback"]["local"][child]
    assert [e["parent_id"] for e in local["evidence"]] == [allowed[0]]
    kb.complete("feedback", TimeOutcome("feedback", 100, 101, "task", "feedback", "0", duration_ms=600))
    feedback = kb.completion_feedback("feedback")
    kb.commit_before(102)
    assert kb.edges[forbidden] == before_bad
    assert kb.edges[allowed].update_count == before_good + 1
    assert kb.predict_duration(full).values_ms == (50, 600, 600)
    replayed = EdgeKappaKB.from_snapshot(baseline)
    replayed.replay_feedback(feedback, kb)
    assert replayed.edges == kb.edges
    assert replayed.predict_duration(full) == kb.predict_duration(full)


def test_edge_own_membership_does_not_disable_coverage_or_kappa_and_roundtrips():
    parent, full, descendant = edge_query(), edge_query("a"), edge_query("a", "b")
    kb = EdgeKappaKB.fit([event(1, parent, 50), event(2, full, 80000), event(3, descendant, 1000)])
    state = kb.nodes[node_id(full.features)]
    assert state.own_observations == {"2"}
    assert state.observations == {"2", "3"}
    prediction = kb.predict_duration(full)
    assert prediction.values_ms == (50, 1000, 80000)
    assert prediction.weights[1:] == (1, 1)
    assert prediction.weights[0] > 0  # own singleton retains parent sharing
    snapshot = kb.to_snapshot()
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts/edge-kappa-kb.schema.json").read_text())
    jsonschema.validate(snapshot, schema)
    restored = EdgeKappaKB.from_snapshot(snapshot)
    assert restored.nodes[node_id(full.features)].own_observations == {"2"}
    assert restored.predict_duration(full) == prediction


def test_old_feedback_with_cross_entry_probabilities_keeps_counts_without_bad_gradient():
    generic, full = edge_query(entry=None), edge_query("a")
    kb = EdgeKappaKB.fit([event(1, generic, 50), event(2, full, 600)])
    kb.begin(full, "legacy", 100)
    child = node_id(full.features)
    # Reproduce a pre-fix local prediction and its forbidden-parent evidence.
    old_query = FeatureQuery(full.features, generic.core_features, "python")
    kb._tokens["legacy"]["local"][child] = asdict(kb.predict(old_query))
    kb.complete("legacy", TimeOutcome("legacy", 100, 101, "task", "legacy", "0", duration_ms=600))
    restored = EdgeKappaKB.from_snapshot(kb.to_snapshot())
    bad_edges = {key: copy.deepcopy(value) for key, value in restored.edges.items()
                 if key[1] == child}
    restored.commit_before(102)
    assert all(restored.edges[key] == value for key, value in bad_edges.items())
    assert restored.nodes[child].own_observations == {"2", "legacy"}


def test_restored_inflight_cache_does_not_serve_legacy_cross_entry_evidence():
    argv = ("python", "-m", "pytest")
    runtime = EdgeKappaRuntime.fit([clause(("python", "-c", "pass"))],
                                   (100, 500, 2000, 10000))
    runtime.predict_load_samples("r", [query_clause(argv)], 1000, call_id="pending")
    query = feature_query(CommandFeatureService().argv(argv, ParseContext(repo="r")))
    old_query = replace(query, core_features=frozenset({"tool=python"}))
    old_prediction = runtime.kb.predict(old_query)
    assert old_prediction.evidence
    runtime.kb._tokens["pending:0"]["prediction"] = asdict(old_prediction)
    runtime._calls["pending"]["pending:0"]["evidence"]["duration_ms"] = {
        "values": (1000,), "weights": (1.0,),
    }
    restored = EdgeKappaRuntime.from_snapshot(runtime.to_snapshot())
    snapshot = restored.to_snapshot()
    reason = "cached_prediction_crosses_execution_entry"
    prediction, _ = restored.kb.begin(query, "pending:0", 1000)
    assert prediction.unavailable_reason == reason
    assert prediction.probabilities is None
    result = restored.predict_load_samples("r", [query_clause(argv)], 1000, call_id="pending")
    assert result[0]["duration_ms"] == {"values": (), "unavailable_reason": reason}
    assert restored.to_snapshot() == snapshot
