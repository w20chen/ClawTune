"""Cold-start KB export.

Serializes the KBs trained on the legacy training split (default: the 80-task
seed-42 split) into the exact snapshot format the project's runtime loads as
its cold-start seed under ``traces/tool-resource/``:

* ``clause-resource-kb.json``       (schema ``runtime_clause_resource_kb_v4``)
* ``clause-lattice-time-kb.json``   (schema ``clause_lattice_time_kb_v1``)
* ``runtime-tool-resource-kb.json`` (schema ``runtime_tool_resource_kb_v1``)

These filenames and schemas are validated by
``swe_rebench.host_sandbox._validate_kb_snapshot_pair`` and loaded by
``ToolResourcePredictor.from_traces`` (via ``artifact_dir``), so an export
written here is picked up automatically as the cold-start seed on the next
benchmark run.

Only the **public (cross-repo) layer** is exported; the per-repo layer is left
empty because the 80 legacy task repos are not the workspaces the project will
run.  The continuous runtime KB additionally keeps the existing seed's
``peak_memory_mb`` global prior: legacy traces carry no ambient-memory anchor,
so a fresh memory prior cannot be learned from them (per user decision).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from legacy_eval._bootstrap import ensure_paths, _REPO_ROOT

ensure_paths()

from tool_resource.runtime_kb import (  # noqa: E402
    ClauseResourceKB,
    RuntimeToolResourceKB,
)
from tool_time.lattice_kb import LatticeTimeKB  # noqa: E402

from legacy_eval.engine import (  # noqa: E402
    EvalConfig,
    build_kbs,
    split_train_test,
)

# The three snapshot filenames the runtime expects (host_sandbox schema map).
CLAUSE_KB_FILENAME = "clause-resource-kb.json"
LATTICE_KB_FILENAME = "clause-lattice-time-kb.json"
RUNTIME_KB_FILENAME = "runtime-tool-resource-kb.json"

_DEFAULT_SEED_DIR = _REPO_ROOT / "traces" / "tool-resource"


@dataclass(frozen=True)
class ColdStartExport:
    """One cold-start KB export (serializable)."""

    out_dir: str
    files: tuple[str, ...]
    train_ids: list[str]
    train_clause_observations: int
    train_tool_calls: int
    clause_public_latency_nodes: int
    lattice_observations: int
    runtime_public_nodes: dict[str, int]
    memory_public_nodes: int
    memory_source: str | None
    clause_fit_error: str | None
    file_sizes_bytes: dict[str, int]
    generated_at_utc: str

    def to_json_obj(self) -> dict[str, Any]:
        return asdict(self)


def _write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_memory_public(path: Path | None) -> dict[tuple[str, str], tuple[float, ...]] | None:
    """Load ``public.peak_memory_mb`` from an existing runtime KB snapshot.

    Returns ``None`` when *path* is absent or the snapshot has no memory nodes.
    """

    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    public = payload.get("public")
    memory_rows = public.get("peak_memory_mb") if isinstance(public, dict) else None
    if not isinstance(memory_rows, list):
        return None
    nodes: dict[tuple[str, str], tuple[float, ...]] = {}
    for row in memory_rows:
        if not isinstance(row, list) or len(row) != 3:
            continue
        kind, key, values = row
        if not isinstance(kind, str) or not isinstance(key, str):
            continue
        if not isinstance(values, list):
            continue
        nodes[(kind, key)] = tuple(float(value) for value in values)
    return nodes or None


def _merge_memory_public(
    runtime_kb: RuntimeToolResourceKB,
    memory_nodes: Mapping[tuple[str, str], tuple[float, ...]] | None,
) -> None:
    """Set the runtime KB's memory public nodes (empty when unavailable)."""

    runtime_kb._public["peak_memory_mb"] = {  # type: ignore[attr-defined]
        key: tuple(values) for key, values in (memory_nodes or {}).items()
    }


def export_cold_start_kb(
    tasks: Mapping[str, Any],
    *,
    config: EvalConfig | None = None,
    out_dir: str | Path | None = None,
    memory_source_path: str | Path | None = None,
) -> ColdStartExport:
    """Train the KBs on the training split and write the three snapshots.

    ``out_dir`` defaults to the project's cold-start seed directory
    ``<repo>/traces/tool-resource``.  ``memory_source_path`` defaults to the
    current seed's ``runtime-tool-resource-kb.json``; its ``peak_memory_mb``
    global prior is preserved (see module docstring).
    """

    if config is None:
        config = EvalConfig()
    if out_dir is None:
        out_dir = _DEFAULT_SEED_DIR
    if memory_source_path is None:
        memory_source_path = _DEFAULT_SEED_DIR / RUNTIME_KB_FILENAME
    out_dir = Path(out_dir)

    train_ids, _, train_clause, train_calls = split_train_test(tasks, config)
    clause_kb, lattice_kb, runtime_kb, clause_fit_error = build_kbs(
        train_clause, train_calls
    )

    memory_nodes = load_memory_public(Path(memory_source_path))
    _merge_memory_public(runtime_kb, memory_nodes)

    files = (
        out_dir / CLAUSE_KB_FILENAME,
        out_dir / LATTICE_KB_FILENAME,
        out_dir / RUNTIME_KB_FILENAME,
    )
    _write_snapshot(files[0], clause_kb.to_json_obj())
    _write_snapshot(files[1], lattice_kb.to_json_obj())
    _write_snapshot(files[2], runtime_kb.to_json_obj())

    file_sizes = {
        path.name: path.stat().st_size for path in files
    }
    return ColdStartExport(
        out_dir=str(out_dir),
        files=tuple(str(path) for path in files),
        train_ids=train_ids,
        train_clause_observations=len(train_clause),
        train_tool_calls=len(train_calls),
        clause_public_latency_nodes=len(
            clause_kb._public["latency_ms"]  # type: ignore[attr-defined]
        ),
        lattice_observations=lattice_kb.observation_count,
        runtime_public_nodes={
            target: len(nodes)
            for target, nodes in runtime_kb._public.items()  # type: ignore[attr-defined]
        },
        memory_public_nodes=len(
            runtime_kb._public["peak_memory_mb"]  # type: ignore[attr-defined]
        ),
        memory_source=(
            str(memory_source_path)
            if memory_source_path is not None
            else None
        ),
        clause_fit_error=clause_fit_error,
        file_sizes_bytes=file_sizes,
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def export_cold_start_kb_dataset(
    dataset_dir: str | Path,
    *,
    config: EvalConfig | None = None,
    out_dir: str | Path | None = None,
    memory_source_path: str | Path | None = None,
) -> ColdStartExport:
    """Convenience: load every task under *dataset_dir*, then export."""

    from legacy_eval.loader import load_all

    tasks = load_all(dataset_dir)
    return export_cold_start_kb(
        tasks,
        config=config,
        out_dir=out_dir,
        memory_source_path=memory_source_path,
    )


__all__ = [
    "CLAUSE_KB_FILENAME",
    "ColdStartExport",
    "LATTICE_KB_FILENAME",
    "RUNTIME_KB_FILENAME",
    "export_cold_start_kb",
    "export_cold_start_kb_dataset",
    "load_memory_public",
]
