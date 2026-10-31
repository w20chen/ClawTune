"""Content-verified derived startup cache for immutable predictor snapshots."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
from uuid import uuid4
from typing import Any

from command_parser.registry import RuleRegistry
from kb_common import require_same_feature_limit, validate_max_optional_features
from tool_time.lattice_kb import LatticeTimeKB


CACHE_FILE = "prepared-predictor-cache.pkl"
CACHE_MANIFEST = "prepared-predictor-cache.json"
LATTICE_FILE = "clause-lattice-time-kb.json"
SCHEMA = "clawtune.prepared-predictor-cache.v4"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def build_prepared_cache(
    artifact_dir: Path, *, max_optional_features: int,
) -> dict[str, Any]:
    maximum = validate_max_optional_features(max_optional_features)
    sources = prepared_cache_sources(artifact_dir)
    lattice_path = artifact_dir / LATTICE_FILE
    lattice = LatticeTimeKB.from_json_obj(json.loads(lattice_path.read_text(encoding="utf-8")))
    require_same_feature_limit(
        lattice.max_optional_features, maximum, source="Lattice startup cache",
    )
    lattice.prepare()
    command_registry = RuleRegistry()
    return save_prepared_cache(artifact_dir, lattice=lattice,
        command_rules=command_registry.prepared_snapshot(), sources=sources,
        max_optional_features=maximum)


def prepared_cache_sources(artifact_dir: Path) -> dict[str, Any]:
    return {"source_sha256": {LATTICE_FILE: _digest(artifact_dir / LATTICE_FILE)},
            "command_rules_sha256": RuleRegistry.bundled_fingerprint()}


def save_prepared_cache(
    artifact_dir: Path, *, lattice: LatticeTimeKB, command_rules: dict,
    sources: dict[str, Any], max_optional_features: int, skip_if_current: bool = False,
) -> dict[str, Any]:
    """Save an already-prepared snapshot; never parse, train or rebuild it."""
    maximum = validate_max_optional_features(max_optional_features)
    require_same_feature_limit(lattice.max_optional_features, maximum, source="Lattice startup cache")
    identity = {"schema": SCHEMA, "python_cache_tag": sys.implementation.cache_tag,
                "max_optional_features": maximum, **sources}
    manifest_path = artifact_dir / CACHE_MANIFEST
    if skip_if_current and (artifact_dir / CACHE_FILE).is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if all(existing.get(key) == value for key, value in identity.items()):
                return existing
        except (OSError, ValueError, AttributeError):
            pass
    payload = pickle.dumps(
        {
            "schema": SCHEMA,
            "lattice": lattice,
            "command_rules": command_rules,
        },
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    if sources != prepared_cache_sources(artifact_dir):
        raise ValueError("startup cache sources changed during preparation")
    cache_path = artifact_dir / CACHE_FILE
    _atomic_bytes(cache_path, payload)
    manifest = {
        **identity,
        "cache_sha256": hashlib.sha256(payload).hexdigest(),
        "cache_bytes": len(payload),
    }
    _atomic_json(artifact_dir / CACHE_MANIFEST, manifest)
    return manifest


def load_prepared_cache(
    artifact_dir: Path | None, *, max_optional_features: int, sources: dict[str, Any] | None = None,
) -> tuple[LatticeTimeKB, RuleRegistry] | None:
    if artifact_dir is None:
        return None
    configured_seed = os.getenv("CLAWTUNE_KB_SEED")
    cache_dir = (
        artifact_dir
        if (artifact_dir / CACHE_FILE).is_file()
        else Path(configured_seed) if configured_seed else artifact_dir
    )
    cache_path = cache_dir / CACHE_FILE
    manifest_path = cache_dir / CACHE_MANIFEST
    if not cache_path.is_file() or not manifest_path.is_file():
        return None
    try:
        sources = sources if sources is not None else prepared_cache_sources(artifact_dir)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        maximum = validate_max_optional_features(max_optional_features)
        if (
            manifest.get("schema") != SCHEMA
            or manifest.get("python_cache_tag") != sys.implementation.cache_tag
            or manifest.get("max_optional_features") != maximum
            or manifest.get("command_rules_sha256") != sources["command_rules_sha256"]
        ):
            return None
        cache_bytes = cache_path.read_bytes()
        if manifest.get("cache_sha256") != hashlib.sha256(cache_bytes).hexdigest():
            return None
        expected = manifest.get("source_sha256")
        if expected != sources["source_sha256"]:
            return None
        payload = pickle.loads(cache_bytes)
        lattice = payload.get("lattice") if isinstance(payload, dict) else None
        command_rules = payload.get("command_rules") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != SCHEMA
            or not isinstance(lattice, LatticeTimeKB)
        ):
            return None
        require_same_feature_limit(
            lattice.max_optional_features, maximum, source="Cached Lattice KB",
        )
        registry = RuleRegistry.from_prepared_snapshot(command_rules)
        return lattice, registry
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError,
            ImportError, pickle.UnpicklingError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--max-optional-features", type=int, default=6)
    args = parser.parse_args()
    print(json.dumps(build_prepared_cache(
        args.artifact_dir,
        max_optional_features=args.max_optional_features,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
