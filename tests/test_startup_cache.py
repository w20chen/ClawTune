from __future__ import annotations

import json
from pathlib import Path

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
