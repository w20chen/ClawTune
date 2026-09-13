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
        assert row["cpu_peak_cores"] is None


def test_rebuild_from_explicit_source_is_deterministic_and_sanitized(tmp_path):
    # Exercise the recipe without requiring deleted blobs in shallow CI clones.
    rows = [{"repo": f"private/project-{i}", "bin": bin_,
             "argv": [bin_, "/private/task-specific-path"], "latency_ms": i + offset,
             "cpu_ns_cumulative": 1_000_000, "sampled_peak_rss_mb": 1}
            for bin_ in BINS for i in range(1, 17) for offset in (0, 20, 100)]
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"schema": "clause_lattice_kb_v2",
                                  "pending": [], "observations": rows}))
    output, repeated = tmp_path / "rebuilt", tmp_path / "repeated"
    build(output, source)
    build(repeated, source)
    for name in (*FILES, "manifest.json"):
        assert (output / name).read_bytes() == (repeated / name).read_bytes()
        assert b"private" not in (output / name).read_bytes()
    lattice = json.loads((output / FILES[2]).read_text())
    assert len(lattice["observations"]) == 40
    for bin_ in BINS:
        selected = [row for row in lattice["observations"] if row["bin"] == bin_]
        assert [row["latency_ms"] for row in selected] == list(range(22, 37, 2))
        assert all(row["repo"] == "" and row["argv"] == [bin_] for row in selected)
    assert validate_seed(output)["provenance"]["source_snapshot_sha256"] == digest(source)


def test_missing_archived_source_explains_explicit_input(tmp_path, monkeypatch):
    import subprocess

    def missing(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0], stderr=b"missing object")

    monkeypatch.setattr("scripts.build_bootstrap_seed.subprocess.check_output", missing)
    with pytest.raises(ValueError, match="Use --source"):
        build(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


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
