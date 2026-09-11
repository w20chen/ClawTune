import json
from pathlib import Path

import pytest

from benchmarks.bootstrap import ROOT
from clawtune_kb import FILES, initialize_state, validate_seed
from clawtune_kb.store import digest
from scripts.build_bootstrap_seed import BINS, build, select_observations
from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB, ToolCallQuery
from tool_time.lattice_kb import LatticeTimeKB


SEED = ROOT / "seeds/bootstrap-v1"


def test_release_seed_is_small_and_has_no_repository_knowledge():
    manifest = validate_seed(SEED)
    assert manifest["provenance"]["observation_count"] == 40
    assert sum(p.stat().st_size for p in SEED.glob("*.json")) < 32_000
    trie, tool, lattice = [json.loads((SEED / name).read_text(encoding="utf-8")) for name in FILES]
    for snapshot in (trie, tool):
        assert snapshot["repo"] == {}
        assert snapshot["pending"] == []
        assert snapshot["observed_counts"] == snapshot["legacy_counts"] == {}
    assert all(not samples for samples in tool["public"].values())
    assert lattice["pending"] == []
    assert len(lattice["observations"]) == 40
    for row in lattice["observations"]:
        assert row["repo"] == ""
        assert row["bin"] in BINS
        assert row["argv"] == [row["bin"]]
        if row["latency_ms"] < 20:
            assert row["cpu_ns_cumulative"] is row["sampled_peak_rss_mb"] is None
        assert row["peak_cpu_cores"] is None


def test_rebuild_from_archived_source_is_byte_identical(tmp_path):
    output = tmp_path / "rebuilt"
    build(output)
    for name in (*FILES, "manifest.json"):
        assert (output / name).read_bytes() == (SEED / name).read_bytes()


def test_bootstrap_predictions_are_identical_for_unrelated_repositories():
    trie = ClauseResourceKB.from_json_obj(json.loads((SEED / FILES[0]).read_text()))
    lattice = LatticeTimeKB.from_json_obj(json.loads((SEED / FILES[2]).read_text()))
    trie.freeze()
    lattice.freeze()
    clauses = [{"bin": "find", "argv": ["find", ".", "-name", "*.py"]}]
    assert trie.predict_load_samples("swe-rebench:a/b", clauses, 1) == trie.predict_load_samples("terminal-bench:unseen", clauses, 1)
    first = lattice.predict_clauses("swe-rebench:a/b", clauses, 1)
    second = lattice.predict_clauses("terminal-bench:unseen", clauses, 1)
    assert first == second
    assert any(p.prediction_ms is not None for p in first[0].predictions)
    assert not any(feature.startswith("repo=") for features in lattice._nodes for feature in features)


def test_initial_state_can_learn_without_mutating_release_seed(tmp_path):
    before = {name: digest(SEED / name) for name in (*FILES, "manifest.json")}
    state = tmp_path / "kb"
    initialize_state(state, SEED, owner="daily")
    trie = ClauseResourceKB.from_json_obj(json.loads((state / FILES[0]).read_text()))
    from tool_resource.runtime_kb import ClauseObservation
    trie.observe_completed_clause(ClauseObservation(
        repo="new-project", bin="find", argv=("find", "."), ts_start=1, ts_end=2, latency_ms=1000))
    trie.predict_load_samples("new-project", [{"bin": "find", "argv": ["find", "."]}], 3)
    assert "new-project" in trie.to_json_obj()["repo"]
    assert {name: digest(SEED / name) for name in before} == before


def test_recipe_rejects_insufficient_source_diversity():
    with pytest.raises(ValueError, match="independent source repositories"):
        select_observations({"observations": []})


def test_cli_default_uses_release_seed():
    from benchmarks.cli import parser
    assert parser().parse_args(["benchmark"]).seed == SEED


def test_release_prior_does_not_claim_canonical_predictions_for_unknown_tools():
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.prediction_config import load_bucket_edges
    payloads = [json.loads((SEED / name).read_text()) for name in FILES]
    prediction, _ = predict_call_load(
        runtime=RuntimeToolResourceKB.from_json_obj(payloads[1]),
        trie=ClauseResourceKB.from_json_obj(payloads[0]),
        lattice=LatticeTimeKB.from_json_obj(payloads[2]),
        query=ToolCallQuery("new/project", "exec", "python train.py", 1),
        edges=load_bucket_edges((100, 500, 2000, 10000)),
        parsed_clauses=[{"bin": "python", "argv": ["python", "train.py"]}],
    )
    assert all(target.status == "unavailable" for target in prediction.targets.values())
