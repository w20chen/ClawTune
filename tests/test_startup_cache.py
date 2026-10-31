from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from clawtune_sidecar.predictors.startup_cache import (
    LATTICE_FILE,
    build_prepared_cache,
    load_prepared_cache,
)
from tool_time.lattice_kb import LatticeTimeKB


def _write_inputs(path: Path) -> None:
    (path / LATTICE_FILE).write_text(json.dumps(LatticeTimeKB().to_json_obj()))


def test_prepared_cache_is_loaded_only_for_exact_source_snapshots(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    manifest = build_prepared_cache(tmp_path, max_optional_features=6)
    cached = load_prepared_cache(tmp_path, max_optional_features=6)
    assert manifest["cache_bytes"] > 0
    assert cached is not None
    assert cached[0].max_optional_features == 6
    assert cached[1].prepared_snapshot()["rules"]

    (tmp_path / LATTICE_FILE).write_text(
        (tmp_path / LATTICE_FILE).read_text() + "\n",
    )
    assert load_prepared_cache(tmp_path, max_optional_features=6) is None


def test_immutable_seed_cache_serves_private_state_without_copy(
    tmp_path: Path, monkeypatch,
) -> None:
    seed = tmp_path / "seed"
    state = tmp_path / "state"
    seed.mkdir()
    state.mkdir()
    _write_inputs(seed)
    build_prepared_cache(seed, max_optional_features=6)
    for name in (LATTICE_FILE,):
        (state / name).write_bytes((seed / name).read_bytes())
    monkeypatch.setenv("CLAWTUNE_KB_SEED", str(seed))

    assert load_prepared_cache(state, max_optional_features=6) is not None
    assert not (state / "prepared-predictor-cache.pkl").exists()


def test_invalid_cache_falls_back_without_unpickling(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    build_prepared_cache(tmp_path, max_optional_features=6)
    (tmp_path / "prepared-predictor-cache.pkl").write_bytes(b"not a pickle")
    assert load_prepared_cache(tmp_path, max_optional_features=6) is None


@pytest.mark.parametrize("change", ["edit", "add", "remove"])
def test_cache_rejects_changed_bundled_rules_before_unpickling(tmp_path, monkeypatch, change):
    from command_parser import registry
    from clawtune_sidecar.predictors import startup_cache

    rule = json.loads((Path(registry.__file__).parent / "rules/cat.json").read_text())
    package = tmp_path / "package"
    (package / "rules").mkdir(parents=True)
    path = package / "rules/cat.json"
    path.write_text(json.dumps(rule))
    monkeypatch.setattr(registry, "__file__", str(package / "registry.py"))
    _write_inputs(tmp_path)
    build_prepared_cache(tmp_path, max_optional_features=6)
    assert load_prepared_cache(tmp_path, max_optional_features=6) is not None
    if change == "edit":
        rule["names"].append("cat-alias")
        path.write_text(json.dumps(rule))
    elif change == "add":
        (package / "rules/extra.json").write_text(json.dumps(rule))
    else:
        path.unlink()
    with patch.object(startup_cache.pickle, "loads", side_effect=AssertionError("stale cache unpickled")):
        assert load_prepared_cache(tmp_path, max_optional_features=6) is None


def test_incompatible_cache_version_is_rejected_before_unpickling(tmp_path, monkeypatch):
    from clawtune_sidecar.predictors import startup_cache

    _write_inputs(tmp_path)
    build_prepared_cache(tmp_path, max_optional_features=6)
    path = tmp_path / startup_cache.CACHE_MANIFEST
    manifest = json.loads(path.read_text())
    manifest["schema"] = "clawtune.prepared-predictor-cache.v2"
    path.write_text(json.dumps(manifest))
    def forbidden(*args):
        raise AssertionError("incompatible cache must not be deserialized")
    monkeypatch.setattr(startup_cache.pickle, "loads", forbidden)
    assert load_prepared_cache(tmp_path, max_optional_features=6) is None


@pytest.mark.parametrize("cache_state", ["valid", "missing", "corrupt", "stale", "old_version", "other_python"])
def test_managed_startup_skips_json_only_when_cache_is_valid(tmp_path, cache_state):
    from clawtune_kb.contracts import data_root
    from clawtune_kb.store import StateStore, initialize_state
    from clawtune_sidecar.predictors import startup_cache
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    from tool_resource.runtime_kb import LatencyBuckets

    state = tmp_path / "state"
    initialize_state(state, data_root() / "seeds/bootstrap-v2", owner="test")
    build_prepared_cache(state, max_optional_features=6)
    if cache_state == "missing":
        (state / startup_cache.CACHE_FILE).unlink()
    elif cache_state == "corrupt":
        (state / startup_cache.CACHE_FILE).write_bytes(b"bad pickle")
    elif cache_state == "stale":
        with StateStore(state) as store:
            with (state / LATTICE_FILE).open("a") as output:
                output.write("\n")
            store.checkpoint()
    elif cache_state in {"old_version", "other_python"}:
        path = state / startup_cache.CACHE_MANIFEST
        manifest = json.loads(path.read_text())
        manifest["schema" if cache_state == "old_version" else "python_cache_tag"] = "obsolete"
        path.write_text(json.dumps(manifest))
    reads = []
    original = Path.open
    def read(path, *args, **kwargs):
        if path.name == startup_cache.CACHE_FILE:
            reads.append(path)
        return original(path, *args, **kwargs)
    with patch.object(LatticeTimeKB, "from_json_obj", wraps=LatticeTimeKB.from_json_obj) as restore, \
         patch.object(Path, "open", read):
        predictor = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
            artifact_dir=state, buckets=LatencyBuckets((100, 500, 2000)))
    try:
        assert restore.call_count == (0 if cache_state == "valid" else 1)
        assert len(reads) <= 1
        assert predictor.lattice_kb.observation_count > 0
    finally:
        predictor.close()
    # Clean shutdown preserves a cache matching the final committed snapshots.
    with patch.object(LatticeTimeKB, "from_json_obj", side_effect=AssertionError("cache was not reused")):
        restarted = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
            artifact_dir=state, buckets=LatencyBuckets((100, 500, 2000)))
    restarted.close()


def _managed_predictor(path):
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    from tool_resource.runtime_kb import LatencyBuckets
    return ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
        artifact_dir=path, buckets=LatencyBuckets((100, 500, 2000)))


def _initialize(path):
    from clawtune_kb.contracts import data_root
    from clawtune_kb.store import initialize_state
    initialize_state(path, data_root() / "seeds/bootstrap-v2", owner="test")


def test_automatic_cache_reuses_preparation_and_preserves_seed(tmp_path, monkeypatch):
    import shutil
    from clawtune_kb.contracts import data_root
    from clawtune_kb.store import initialize_state
    from clawtune_sidecar.predictors import startup_cache

    seed, state = tmp_path / "seed", tmp_path / "state"
    shutil.copytree(data_root() / "seeds/bootstrap-v2", seed)
    seed_bytes = {p.name: p.read_bytes() for p in seed.iterdir() if p.is_file()}
    monkeypatch.setenv("CLAWTUNE_KB_SEED", str(seed))
    initialize_state(state, seed, owner="test")
    with patch.object(startup_cache, "build_prepared_cache", side_effect=AssertionError("second build")), \
         patch.object(LatticeTimeKB, "prepare", autospec=True, side_effect=LatticeTimeKB.prepare) as prepare:
        p = _managed_predictor(state)
        assert (state / startup_cache.CACHE_FILE).is_file()
        before = prepare.call_count
        p.close()
        assert prepare.call_count == before  # shutdown only serializes prepared state
    assert {p.name: p.read_bytes() for p in seed.iterdir() if p.is_file()} == seed_bytes
    assert load_prepared_cache(state, max_optional_features=6) is not None


@pytest.mark.parametrize("failure", ["permission", "serialize", "manifest"])
def test_optional_cache_failure_does_not_block_startup_or_close(tmp_path, monkeypatch, failure):
    from clawtune_sidecar.predictors import startup_cache

    state = tmp_path / "state"
    _initialize(state)
    original = startup_cache._atomic_bytes
    def fail_write(path, payload):
        if failure == "permission" or path.name == startup_cache.CACHE_MANIFEST:
            raise PermissionError("read-only cache destination")
        original(path, payload)
    with monkeypatch.context() as m:
        if failure == "serialize":
            m.setattr(startup_cache.pickle, "dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("unpickleable")))
        else:
            m.setattr(startup_cache, "_atomic_bytes", fail_write)
        p = _managed_predictor(state)
        assert p.lattice_kb.observation_count > 0
        p.close()
        assert p._closed and p._state_store is None
    # A partial cache must be rejected and automatically replaced next time.
    p = _managed_predictor(state)
    p.close()
    assert load_prepared_cache(state, max_optional_features=6) is not None


def test_shutdown_cache_contains_new_learning_but_not_generated_rules(tmp_path):
    from copy import deepcopy
    from command_parser.models import ParseContext
    from tool_resource.runtime_kb import ClauseObservation

    state = tmp_path / "state"
    _initialize(state)
    p = _managed_predictor(state)
    before = p.lattice_kb.observation_count + p.lattice_kb.pending_count
    extra = ClauseObservation("cache-review", "sleep", ("sleep", "9"), 100, 109, latency_ms=9000)
    p._kb_writes.enqueue((extra,))
    rule, _ = p.command_features.registry.lookup("cat", ParseContext())
    rule = deepcopy(rule)
    rule["names"].append("runtime-only-alias")
    p.command_features.registry.add(rule, persist=False)
    p.close()
    with patch.object(LatticeTimeKB, "from_json_obj", side_effect=AssertionError("cache miss after learning")):
        p = _managed_predictor(state)
    try:
        assert p.lattice_kb.observation_count + p.lattice_kb.pending_count == before + 1
        assert p.command_features.registry.lookup("runtime-only-alias", ParseContext())[0] is None
    finally:
        p.close()


def test_cache_publication_rejects_changed_source(tmp_path, monkeypatch):
    from clawtune_sidecar.predictors import startup_cache

    _write_inputs(tmp_path)
    original = startup_cache.pickle.dumps
    def changed(*args, **kwargs):
        with (tmp_path / LATTICE_FILE).open("a") as output:
            output.write("\n")
        return original(*args, **kwargs)
    monkeypatch.setattr(startup_cache.pickle, "dumps", changed)
    with pytest.raises(ValueError, match="sources changed"):
        build_prepared_cache(tmp_path, max_optional_features=6)
    assert not (tmp_path / startup_cache.CACHE_MANIFEST).exists()


def test_unchanged_restart_does_not_serialize_cache_again(tmp_path):
    from clawtune_sidecar.predictors import startup_cache

    state = tmp_path / "state"
    _initialize(state)
    p = _managed_predictor(state)
    p.close()
    before = (state / startup_cache.CACHE_FILE).stat().st_mtime_ns
    with patch.object(startup_cache.pickle, "dumps", side_effect=AssertionError("unnecessary serialization")) as dumps:
        p = _managed_predictor(state)
        p.close()
    dumps.assert_not_called()
    assert (state / startup_cache.CACHE_FILE).stat().st_mtime_ns == before


def test_bounded_shutdown_skips_optional_cache_write(tmp_path):
    state = tmp_path / "state"
    _initialize(state)
    p = _managed_predictor(state)
    with patch.object(p, "_save_startup_cache") as save:
        p.close(timeout_seconds=30)
    save.assert_not_called()
    assert p._closed and p._state_store is None
