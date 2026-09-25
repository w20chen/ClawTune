"""Recorded clause evaluation must not depend on a shell parser or Go."""
import json

import pytest

from cold_start.flat_loader import read_task, recorded_clause_structure
from clawtune_kb.store import digest
from offline.edge_kappa_trace import read_v5_events
from tool_resource import features, mvdan_client
from test_cold_start import trace


@pytest.fixture(autouse=True)
def no_parser(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("recorded clause path invoked mvdan")
    monkeypatch.setattr(features, "parse_command_clauses", forbidden)
    monkeypatch.setattr(mvdan_client.MvdanClient, "parse", forbidden)
    monkeypatch.setattr(mvdan_client, "ensure_compatible_adapter", forbidden)


def write_trace(tmp_path, changes):
    rows = [json.loads(line) for line in trace("task").splitlines()]
    clause = rows[1]["data"]["resource_observation"]["clauses"][0]
    clause.update(in_loop=False, in_pipe=False, in_subst=False, pipeline_position=-1)
    clause.update(changes)
    path = tmp_path / "task.trace.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


@pytest.mark.parametrize("strict", [False, True])
def test_loader_preserves_recorded_structure_without_parser(tmp_path, strict):
    path = write_trace(tmp_path, {"in_loop": True, "in_subst": True,
                                  "in_pipe": True, "pipeline_position": 0})
    loaded = read_task(path, repo="repo", task_id="task", rss_unit="MiB",
                       recorded_clauses_only=strict)
    assert len(loaded.clauses) == 1
    row = loaded.clauses[0]
    assert row.in_loop and row.in_subst and row.in_pipe
    assert row.pipeline_position == 0
    assert row.latency_ms == 1000
    assert loaded.call_clauses[0][0]["argv"] == ["python", "work.py"]


@pytest.mark.parametrize("field", ["in_loop", "in_pipe", "in_subst", "pipeline_position"])
@pytest.mark.parametrize("invalid", ["missing", None, "false"])
def test_strict_structure_rejects_missing_or_invalid_fields(field, invalid):
    row = dict(in_loop=False, in_pipe=False, in_subst=False, pipeline_position=-1)
    if invalid == "missing":
        del row[field]
    else:
        row[field] = invalid
    with pytest.raises(ValueError, match="requires recorded"):
        recorded_clause_structure("python work.py", [row], recorded_only=True)


@pytest.mark.parametrize("reader", ["flat", "trace"])
def test_readers_reject_incomplete_structure_without_parser(tmp_path, reader):
    path = write_trace(tmp_path, {"in_pipe": None})
    with pytest.raises(ValueError, match="requires recorded"):
        if reader == "flat":
            read_task(path, repo="repo", task_id="task", rss_unit="MiB",
                      recorded_clauses_only=True)
        else:
            task = {"benchmark": "bfcl", "group": "repo", "task_id": "task",
                    "files": [{"version": 5, "path": path.name, "sha256": digest(path)}]}
            read_v5_events(tmp_path, task, "bfcl:task")


def test_compatibility_loader_can_still_backfill_old_records(monkeypatch):
    sentinel = ({"in_pipe": True},)
    monkeypatch.setattr(features, "enrich_clause_structure", lambda command, rows: sentinel)
    assert recorded_clause_structure("a | b", [{}]) is sentinel


def test_empty_recorded_clauses_do_not_require_parser():
    assert recorded_clause_structure("", [], recorded_only=True) == ()


def write_v6(tmp_path, task_id="task", *, changes=None, censored=False, eligible=True):
    from test_kb_ingress_parity import clause
    from clawtune_sidecar.predictors.tool_resource import _compact_call_telemetry
    row = clause()
    row.update(in_loop=False, in_pipe=False, in_subst=False, pipeline_position=-1)
    call = _compact_call_telemetry({"command": "python work.py", "tool_call_id": "call",
        "eligible_for_kb": eligible, "telemetry_quality": "ok", "clauses": [row]})
    call["clauses"][0].update(changes or {})
    end = {"record_type": "span_end", "kind": "tool", "span_id": "call", "name": "exec",
           "wall_time_ns": "2000000000", "duration_ns": "2000000000",
           "status": {"code": "error" if censored else "ok",
                      "message": "timeout" if censored else None},
           "execution": {"execution_id": "call", "tool_resource": {"call_telemetry": call}}}
    metadata = {"record_type": "trace_metadata", "schema_version": 6,
                "instance_id": task_id, "benchmark": "bfcl"}
    path = tmp_path / f"{task_id}.trace.jsonl"
    path.write_text("\n".join(map(json.dumps, [metadata, end])), encoding="utf-8")
    task = {"benchmark": "bfcl", "group": "dataset", "task_id": task_id,
            "files": [{"version": 6, "path": path.name, "sha256": digest(path)}]}
    return task


@pytest.mark.parametrize("strict", [False, True])
def test_v6_compact_structure_and_labels_preserved(tmp_path, strict):
    from offline.runner import load_task
    task = write_v6(tmp_path, changes={"in_loop": True, "in_subst": True,
                                      "in_pipe": True, "pipeline_position": 0})
    loaded = load_task(tmp_path, task, "MB", recorded_clauses_only=strict)
    assert len(loaded.clauses) == 1
    row = loaded.clauses[0]
    assert row.in_loop and row.in_subst and row.in_pipe
    assert row.pipeline_position == 0
    assert row.latency_ms == 1000
    assert row.cpu_ns_cumulative == 500_000_000
    assert row.sampled_peak_rss_mb == 8
    assert loaded.call_actuals[0]["duration_ms"] == 1000


@pytest.mark.parametrize("changes", [{"in_pipe": None}, {"pipeline_position": True}])
def test_v6_strict_structure_fails_without_parser(tmp_path, changes):
    from offline.runner import load_task
    task = write_v6(tmp_path, changes=changes)
    with pytest.raises(ValueError, match="requires recorded"):
        load_task(tmp_path, task, "MB", recorded_clauses_only=True)


@pytest.mark.parametrize("gates", [{"censored": True}, {"eligible": False}])
def test_v6_quality_gates_precede_structure_validation(tmp_path, gates):
    from offline.runner import load_task
    task = write_v6(tmp_path, changes={"in_pipe": None}, **gates)
    assert not load_task(tmp_path, task, "MB", recorded_clauses_only=True).clauses


@pytest.mark.parametrize("online", [False, True])
def test_v6_end_to_end_clause_evaluation_without_mvdan(tmp_path, online):
    from offline.edge_kappa_eval import run
    dataset = tmp_path / "input"
    dataset.mkdir()
    for index in range(5):
        write_v6(dataset, f"task-{index}")
    report = run(dataset, tmp_path / "output", benchmark="bfcl", online=online,
                 split_cache_dir=tmp_path / "splits")
    assert report["train_clauses"] == 4
    assert report["test_clauses"] == 1
    assert report["metrics"]["predicted"] == 1
    assert report["test_updates"] == int(online)


def test_v6_writer_preserves_structure_and_does_not_invent_missing_fields():
    from clawtune_sidecar.predictors.tool_resource import _compact_clauses
    structure = dict(in_loop=True, in_pipe=True, in_subst=True, pipeline_position=2)
    compact = _compact_clauses([dict(bin="python", argv=["python"], **structure),
                                dict(bin="python", argv=["python"])])
    assert {key: compact[0][key] for key in structure} == structure
    assert all(key not in compact[1] for key in structure)
