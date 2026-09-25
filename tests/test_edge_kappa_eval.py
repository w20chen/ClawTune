from __future__ import annotations

import math
import json
from pathlib import Path
import pytest

from cold_start.flat_loader import LoadedTask
from tool_resource.runtime_kb import ClauseObservation
from offline import edge_kappa_eval
from offline.edge_kappa_compare import compare, compare_buckets
from offline.edge_kappa_trace import read_v5_events
from offline.edge_kappa_legacy import run as legacy_run
from clawtune_kb.store import digest
from offline.edge_kappa_eval import _score
from edge_kappa_kb import FeatureQuery, TimeOutcome, TrainingEvent


def test_edge_kappa_metrics_recompute_from_prediction_rows() -> None:
    rows = [
        {"actual_bucket": 0, "predicted_bucket": 0, "probabilities": (0.8, 0.2)},
        {"actual_bucket": 1, "predicted_bucket": 0, "probabilities": (0.6, 0.4)},
        {"actual_bucket": 1, "predicted_bucket": None, "probabilities": None},
    ]
    result = _score(rows, 2)
    assert result["labeled"] == 3
    assert result["predicted"] == 2
    assert result["coverage"] == 2 / 3
    assert result["log_loss"] == (-math.log(.8) - math.log(.4)) / 2
    assert result["brier"] == pytest.approx(((.2**2 + .2**2) + (.6**2 + .6**2)) / 2)
    assert result["accuracy"] == .5
    assert result["macro_recall"] == .5


def test_frozen_and_record_order_online_evaluation_share_training_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = tmp_path / "input"
    dataset.mkdir()
    tasks = {key: {"benchmark": "swe-rebench", "group": "repo", "task_id": key}
             for key in ("train-1", "train-2", "test-1", "test-2")}
    split = {"train": ["train-1", "train-2"], "test": ["test-1", "test-2"],
             "assignment_sha256": "digest"}
    def load(_dataset: Path, task: dict, _unit: str, *, recorded_clauses_only=False) -> LoadedTask:
        assert recorded_clauses_only
        command = ("python", "-m", "pytest", task["task_id"])
        duration = 50 if task["task_id"] == "train-1" else 600
        clause = ClauseObservation("swe-rebench:repo", "python", command, 0, 1,
                                   latency_ms=duration)
        return LoadedTask(clauses=[clause])
    monkeypatch.setattr(edge_kappa_eval, "inventory", lambda *_args: tasks)
    monkeypatch.setattr(edge_kappa_eval, "load_or_create_split", lambda *_args: split)
    monkeypatch.setattr(edge_kappa_eval, "load_task", load)
    frozen = edge_kappa_eval.run(dataset, tmp_path / "frozen", benchmark="swe-rebench")
    online = edge_kappa_eval.run(dataset, tmp_path / "online", benchmark="swe-rebench",
                                 online=True)
    assert frozen["mode"] == "frozen" and frozen["test_updates"] == 0
    assert online["mode"] == "record_order_online" and online["test_updates"] == 2
    assert json.loads((tmp_path / "frozen" / "edge-kappa-kb.json").read_text()) == \
           json.loads((tmp_path / "online" / "edge-kappa-kb.json").read_text())
    assert len((tmp_path / "online" / "predictions.jsonl").read_text().splitlines()) == 2
    paired = compare(tmp_path / "frozen", tmp_path / "online", repetitions=100)
    assert paired["paired_tasks"] == 2
    assert paired["paired_predictions"] == 2


def test_trace_online_replays_task_events_in_start_order(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = tmp_path / "input"
    dataset.mkdir()
    query = FeatureQuery(frozenset({"tool=python"}), frozenset({"tool=python"}),
                         "python")
    def event(event_id: str, start: float, end: float, task: str) -> TrainingEvent:
        return TrainingEvent(query, TimeOutcome(event_id, start, end, task, event_id,
                                                "0", duration_ms=50), "python")
    tasks = {task: {"benchmark": "bfcl", "group": "repo"}
             for task in ("train", "test")}
    split = {"train": ["train"], "test": ["test"], "assignment_sha256": "digest"}
    records = [event("late", 20, 21, "test"), event("latest", 30, 31, "test"),
               event("early", 10, 11, "test")]
    monkeypatch.setattr(edge_kappa_eval, "inventory", lambda *_args: tasks)
    monkeypatch.setattr(edge_kappa_eval, "load_or_create_split", lambda *_args: split)
    monkeypatch.setattr(edge_kappa_eval, "_events", lambda _dataset, _tasks, keys,
                        _rss, _clock: ([event("seed", 0, 1, "train")], {})
                        if keys == ["train"] else (records, {}))
    edge_kappa_eval.run(dataset, tmp_path / "output", benchmark="bfcl",
                        online=True, event_clock="trace")
    rows = [json.loads(line) for line in (tmp_path / "output" / "predictions.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    assert [row["call_id"] for row in rows] == ["early", "late", "latest"]
    assert [row["generation"] for row in rows] == [1, 2, 3]


@pytest.mark.parametrize("online", [False, True])
def test_evaluator_reads_v5_clause_traces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         online: bool) -> None:
    from tool_resource import features, mvdan_client
    def forbidden(*args, **kwargs):
        pytest.fail("offline clause evaluation attempted to use mvdan")
    monkeypatch.setattr(features, "parse_command_clauses", forbidden)
    monkeypatch.setattr(mvdan_client.MvdanClient, "parse", forbidden)
    monkeypatch.setattr(mvdan_client, "ensure_compatible_adapter", forbidden)
    dataset = tmp_path / "traces"
    dataset.mkdir()
    for index in range(5):
        task = f"task-{index}"
        command = "python work.py"
        rows = [
            {"type": "trace_metadata", "trace_format_version": 5,
             "instance_id": task, "benchmark": "bfcl"},
            {"type": "action", "action_type": "tool_exec", "instance_id": task,
             "action_id": "action", "data": {"tool_name": "exec", "tool_call_id": "call",
                "tool_args": json.dumps({"command": command}), "duration_ms": 50 + index * 100,
                "success": True, "resource_observation": {
                    "tool_call_id": "call", "command": command, "eligible_for_kb": True,
                    "telemetry_quality": "ok", "telemetry_status": "ok", "clauses": [{
                        "bin": "python", "argv": ["python", "work.py"],
                        "in_loop": False, "in_pipe": False, "in_subst": False,
                        "pipeline_position": -1,
                "eligible_for_kb": True, "telemetry_quality": "ok",
                "availability": {"latency": "ok"},
                        "latency_ms": 50 + index * 100,
                        "ts_start": 100.0, "ts_end": 100.1}]}}},
        ]
        (dataset / f"{task}.trace.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = edge_kappa_eval.run(dataset, tmp_path / "result", benchmark="bfcl",
                                 split_cache_dir=tmp_path / "split-cache", online=online)
    assert report["train_clauses"] == 4
    assert report["test_clauses"] == 1
    assert report["metrics"]["predicted"] == 1
    strict = edge_kappa_eval.run(dataset, tmp_path / "strict", benchmark="bfcl",
                                 split_cache_dir=tmp_path / "split-cache", event_clock="trace",
                                 online=online)
    assert strict["event_clock"] == "trace"
    assert strict["test_clauses"] == 1
    legacy = legacy_run(dataset, tmp_path / "legacy", benchmark="bfcl",
                        split_cache_dir=tmp_path / "split-cache")
    assert legacy["test_clauses"] == 1
    assert legacy["algorithm"] == "shrinkage"
    assert legacy["bucket_edges_ms"] == [100, 500, 2000, 10000]
    corrected = legacy_run(dataset, tmp_path / "legacy-corrected", benchmark="bfcl",
                           split_cache_dir=tmp_path / "split-cache", corrected_coverage=True)
    assert corrected["corrected_coverage"] is True


def test_trace_adapter_excludes_censored_and_missing_clock(tmp_path: Path) -> None:
    path = tmp_path / "task.trace.jsonl"
    command = "python work.py"
    def action(action_id: str, *, censored: bool = False,
               clock: bool = True) -> dict:
        clause = {"bin": "python", "argv": ["python", "work.py"],
                  "in_loop": False, "in_pipe": False, "in_subst": False,
                  "pipeline_position": -1,
                  "eligible_for_kb": True, "telemetry_quality": "ok",
                  "availability": {"latency": "ok"}, "latency_ms": 600}
        if clock:
            clause.update(ts_start=100.0, ts_end=100.6)
        return {"type": "action", "action_type": "tool_exec", "instance_id": "task",
                "action_id": action_id, "data": {"tool_name": "exec",
                "tool_call_id": action_id, "tool_args": json.dumps({"command": command}),
                "censored": censored, "resource_observation": {
                    "tool_call_id": action_id, "command": command,
                    "eligible_for_kb": True, "telemetry_quality": "ok",
                    "telemetry_status": "ok", "clauses": [clause]}}}
    rows = [action("valid"), action("timeout", censored=True),
            action("no-clock", clock=False)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    task = {"benchmark": "bfcl", "group": "dataset", "task_id": "task",
            "files": [{"version": 5, "path": path.name, "sha256": digest(path)}]}
    events, excluded = read_v5_events(tmp_path, task, "bfcl:task")
    assert len(events) == 1
    assert events[0].outcome.call_id == "valid"
    assert excluded["missing_clause_clock"] == 1


@pytest.mark.parametrize("candidate_edges", ([1000, 5000], None))
def test_bucket_comparison_rejects_incompatible_or_missing_edges(
        tmp_path: Path, candidate_edges: list[int] | None) -> None:
    for name, edges in (("baseline", [100, 500]), ("candidate", candidate_edges)):
        directory = tmp_path / name
        directory.mkdir()
        report = {"split_sha256": "same", "bucket_edges_ms": edges}
        (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        row = {"task": "task", "call_id": "call", "clause_id": "0",
               "actual_bucket": 0, "predicted_bucket": 0}
        (directory / "predictions.jsonl").write_text(
            json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bucket edges"):
        compare_buckets(tmp_path / "baseline", tmp_path / "candidate", repetitions=100)


def test_bucket_comparison_accepts_matching_edges(tmp_path: Path) -> None:
    for name in ("baseline", "candidate"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "report.json").write_text(json.dumps({
            "split_sha256": "same", "bucket_edges_ms": [100, 500],
        }), encoding="utf-8")
        (directory / "predictions.jsonl").write_text(json.dumps({
            "task": "task", "call_id": "call", "clause_id": "0",
            "actual_bucket": 0, "predicted_bucket": 0,
        }) + "\n", encoding="utf-8")
    result = compare_buckets(tmp_path / "baseline", tmp_path / "candidate",
                             repetitions=100)
    assert result["paired_predictions"] == 1
    assert result["difference"]["accuracy"]["estimate"] == 0
