"""Tests for the cold-start KB export (``legacy_eval.export``).

Run from the repo root::

    python -m pytest tests/test_legacy_eval_export.py -q --basetemp .pytest-tmp-root
"""

from __future__ import annotations

import json

from swe_rebench.host_openclaw import _validate_kb_snapshot_pair

from legacy_eval.engine import EvalConfig
from legacy_eval.export import (
    RUNTIME_KB_FILENAME,
    export_cold_start_kb,
    load_memory_public,
)
from legacy_eval.loader import ClauseEvent, TaskArtifacts, ToolCallEvent
from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
from tool_time.lattice_kb import LatticeTimeKB


def _clause_event(repo: str, bin_: str, argv: list[str], latency_ms: float) -> ClauseEvent:
    return ClauseEvent(
        repo=repo,
        bin=bin_,
        argv=tuple(argv),
        latency_ms=latency_ms,
        eligible=True,
        tool_call_id="c0",
        peak_cpu_cores=0.5,
        sampled_peak_rss_mb=None,
    )


def _tool_call(repo: str, command: str, duration_ms: float) -> ToolCallEvent:
    return ToolCallEvent(
        repo=repo,
        tool_name="exec",
        tool_args=json.dumps({"command": command}),
        command=command,
        duration_ms=duration_ms,
        success=True,
        ts_start=0.0,
        ts_end=duration_ms / 1000.0,
        tool_call_id="c0",
        iteration=0,
        peak_cpu_cores=0.5,
    )


def _synthetic_tasks(n_tasks: int = 8) -> dict[str, TaskArtifacts]:
    tasks: dict[str, TaskArtifacts] = {}
    for i in range(n_tasks):
        # Two tasks per repo so a per-repo 50% split gives 4 train / 4 test.
        tid = f"repo{i // 2}__pkg-{i}"
        tasks[tid] = TaskArtifacts(
            task_id=tid,
            task_dir=None,  # type: ignore[arg-type]
            clause_events=[
                _clause_event(tid, "git", ["git", "log"], 50.0 + i),
                _clause_event(tid, "python3", ["python3", "-m", "pytest"], 300.0 + i * 10),
            ],
            tool_calls=[
                _tool_call(tid, "git log", 55.0 + i),
                _tool_call(tid, "python3 -m pytest", 310.0 + i * 10),
            ],
        )
    return tasks


_SEED_RUNTIME_KB = {
    "schema": "runtime_tool_resource_kb_v1",
    "quantile": 0.9,
    "max_prefix_depth": 4,
    "public": {
        "latency_ms": [["global", "", [100.0]]],
        "peak_cpu_cores": [["global", "", [1.0]]],
        "peak_memory_mb": [["global", "", [50.0, 60.0]]],
    },
    "repo": {},
    "pending": [],
    "last_query_ts": None,
}


def _write_seed_runtime_kb(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_SEED_RUNTIME_KB, indent=2), encoding="utf-8")


def test_export_writes_three_valid_snapshots(tmp_path) -> None:
    tasks = _synthetic_tasks(8)
    seed = tmp_path / "seed" / RUNTIME_KB_FILENAME
    _write_seed_runtime_kb(seed)
    out = tmp_path / "out"

    export = export_cold_start_kb(
        tasks,
        config=EvalConfig(train_frac=0.5, seed=1),
        out_dir=out,
        memory_source_path=seed,
    )

    # 8 tasks -> 4 train / 4 test; every train observation is eligible here.
    assert len(export.train_ids) == 4
    assert export.train_clause_observations == 8  # 2 per train task
    assert export.train_tool_calls == 8
    assert export.clause_public_latency_nodes >= 1
    assert export.lattice_observations == 8
    assert export.memory_public_nodes == 1

    # All three files exist with the expected schemas.
    schemas = {
        "clause-resource-kb.json": "runtime_clause_resource_kb_v4",
        "clause-lattice-time-kb.json": "clause_lattice_time_kb_v1",
        "runtime-tool-resource-kb.json": "runtime_tool_resource_kb_v1",
    }
    for filename, schema in schemas.items():
        path = out / filename
        assert path.is_file(), filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema"] == schema

    # The project's own snapshot validator accepts the export.
    _validate_kb_snapshot_pair(out)


def test_export_snapshots_round_trip_through_scheduler(tmp_path) -> None:
    tasks = _synthetic_tasks(8)
    out = tmp_path / "out"
    export = export_cold_start_kb(
        tasks,
        config=EvalConfig(train_frac=0.5, seed=1),
        out_dir=out,
        # A non-existent memory source -> no memory prior in this variant.
        memory_source_path=tmp_path / "missing" / RUNTIME_KB_FILENAME,
    )

    clause = ClauseResourceKB.from_json_obj(
        json.loads((out / "clause-resource-kb.json").read_text(encoding="utf-8"))
    )
    assert clause._public["latency_ms"].get(("global", ""))  # type: ignore[attr-defined]

    runtime = RuntimeToolResourceKB.from_json_obj(
        json.loads((out / "runtime-tool-resource-kb.json").read_text(encoding="utf-8"))
    )
    assert ("global", "") in runtime._public["latency_ms"]  # type: ignore[attr-defined]

    lattice = LatticeTimeKB.from_json_obj(
        json.loads((out / "clause-lattice-time-kb.json").read_text(encoding="utf-8"))
    )
    assert lattice.observation_count == 8

    # Without a memory source, the memory public layer is honestly empty.
    assert export.memory_public_nodes == 0
    assert runtime._public["peak_memory_mb"] == {}  # type: ignore[attr-defined]


def test_export_merges_memory_prior_from_seed(tmp_path) -> None:
    tasks = _synthetic_tasks(8)
    seed = tmp_path / "seed" / RUNTIME_KB_FILENAME
    _write_seed_runtime_kb(seed)
    out = tmp_path / "out"
    export = export_cold_start_kb(
        tasks,
        config=EvalConfig(train_frac=0.5, seed=1),
        out_dir=out,
        memory_source_path=seed,
    )
    runtime = RuntimeToolResourceKB.from_json_obj(
        json.loads((out / "runtime-tool-resource-kb.json").read_text(encoding="utf-8"))
    )
    memory = runtime._public["peak_memory_mb"]  # type: ignore[attr-defined]
    assert ("global", "") in memory
    assert memory[("global", "")] == (50.0, 60.0)
    assert export.memory_public_nodes == 1


def test_load_memory_public(tmp_path) -> None:
    seed = tmp_path / "seed" / RUNTIME_KB_FILENAME
    _write_seed_runtime_kb(seed)
    nodes = load_memory_public(seed)
    assert nodes is not None
    assert nodes[("global", "")] == (50.0, 60.0)
    assert load_memory_public(tmp_path / "missing.json") is None
