"""Tests for the independent legacy-format evaluator (``legacy_eval``).

Run from the repo root::

    python -m pytest tests/test_legacy_eval.py -q --basetemp .pytest-tmp-root
"""

from __future__ import annotations

import json

import pytest

from tool_resource.runtime_kb import CompletedCall, ToolCallQuery
from tool_time.lattice_kb import LATTICE_TIME_ALGORITHMS

from legacy_eval.engine import (
    EvalConfig,
    build_runtime_public,
    evaluate,
    to_clause_observation,
    to_completed_call,
)
from legacy_eval.loader import (
    ClauseEvent,
    TaskArtifacts,
    ToolCallEvent,
    is_task_dir,
    load_task,
    parse_clause_artifact,
    parse_trace,
)
from legacy_eval.metrics import summarize_bucket, summarize_point, summarize_quantile
from legacy_eval.split import split_tasks


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _artifact(calls: list[dict]) -> dict:
    """A legacy clause_telemetry.json body that passes the native validator."""

    return {
        "version": 2,
        "mode": "clause",
        "replay_execution": "completed",
        "cleanup": "ok",
        "telemetry_quality": "ok",
        "collector": {
            "state_before_close": "active",
            "unavailable_call_count": 0,
            "valid_call_count": len(calls),
        },
        "telemetry_loss_total": {"total": 0},
        "calls": calls,
    }


def _call(
    clauses: list[dict],
    *,
    tool_call_id: str = "call_0_0",
    command: str = "echo hi",
    eligible: bool = True,
) -> dict:
    return {
        "tool_call_id": tool_call_id,
        "command": command,
        "eligible_for_kb": eligible,
        "clauses": clauses,
    }


def _clause(
    bin_: str,
    argv: list[str],
    latency_ms: float,
    *,
    eligible: bool = True,
    peak_cpu_cores: float | None = None,
    sampled_peak_rss_mb: float | None = None,
    availability_latency: str = "ok",
) -> dict:
    return {
        "bin": bin_,
        "argv": list(argv),
        "latency_ms": latency_ms,
        "eligible_for_kb": eligible,
        "peak_cpu_cores": peak_cpu_cores,
        "sampled_peak_rss_mb": sampled_peak_rss_mb,
        "availability": {"latency": availability_latency},
    }


def _clause_event(
    repo: str,
    bin_: str,
    argv: list[str],
    latency_ms: float,
    *,
    eligible: bool = True,
    tool_call_id: str | None = None,
    peak_cpu_cores: float | None = None,
) -> ClauseEvent:
    return ClauseEvent(
        repo=repo,
        bin=bin_,
        argv=tuple(argv),
        latency_ms=latency_ms,
        eligible=eligible,
        tool_call_id=tool_call_id,
        peak_cpu_cores=peak_cpu_cores,
        sampled_peak_rss_mb=None,
    )


def _tool_call(
    repo: str,
    *,
    tool_name: str = "exec",
    command: str = "echo hi",
    duration_ms: float = 120.0,
    success: bool = True,
    ts_start: float = 0.0,
    ts_end: float = 0.12,
    tool_call_id: str = "call_0_0",
    peak_cpu_cores: float | None = None,
) -> ToolCallEvent:
    return ToolCallEvent(
        repo=repo,
        tool_name=tool_name,
        tool_args=json.dumps({"command": command}) if command else None,
        command=command,
        duration_ms=duration_ms,
        success=success,
        ts_start=ts_start,
        ts_end=ts_end,
        tool_call_id=tool_call_id,
        iteration=0,
        peak_cpu_cores=peak_cpu_cores,
    )


def _task(task_id: str, clause_events: list[ClauseEvent], tool_calls: list[ToolCallEvent]) -> TaskArtifacts:
    return TaskArtifacts(task_id=task_id, task_dir=None, clause_events=clause_events, tool_calls=tool_calls)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------


def test_split_is_deterministic_disjoint_and_fraction() -> None:
    ids = [f"repo{i}" for i in range(100)]
    train1, test1 = split_tasks(ids, train_frac=0.8, seed=42)
    train2, test2 = split_tasks(ids, train_frac=0.8, seed=42)
    assert train1 == train2
    assert test1 == test2
    assert len(train1) == 80
    assert len(test1) == 20
    assert set(train1).isdisjoint(test1)
    assert set(train1) | set(test1) == set(ids)


def test_split_different_seeds_differ() -> None:
    ids = [f"repo{i}" for i in range(100)]
    _, test_a = split_tasks(ids, seed=1)
    _, test_b = split_tasks(ids, seed=2)
    assert test_a != test_b


def test_split_rejects_bad_fraction() -> None:
    with pytest.raises(ValueError):
        split_tasks(["a"], train_frac=1.0)


# ---------------------------------------------------------------------------
# Loader: clause artifact
# ---------------------------------------------------------------------------


def test_parse_clause_artifact_filters(tmp_path) -> None:
    artifact = _artifact(
        [
            _call(
                [
                    _clause("git", ["git", "log"], 50.0, eligible=True, peak_cpu_cores=0.5),
                    _clause(
                        "git",
                        ["git", "status"],
                        30.0,
                        eligible=False,
                        availability_latency="ok",
                    ),
                    _clause(
                        "bad",
                        ["bad", "cmd"],
                        10.0,
                        availability_latency="unknown:no_evidence",
                    ),
                ],
                tool_call_id="call_0_0",
                command="git log && git status",
            ),
            _call(
                [_clause("python3", ["python3", "-m", "pytest"], 500.0, eligible=True)],
                tool_call_id="call_0_1",
                eligible=False,  # call-level ineligible
            ),
        ]
    )
    path = tmp_path / "clause_telemetry.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    events = parse_clause_artifact(path, "repo__x-1")
    # latency-ok clauses only: first two (call 0), plus the one in call 1.
    assert len(events) == 3
    by_argv = {tuple(ev.argv): ev for ev in events}
    assert by_argv[("git", "log")].latency_ms == 50.0
    assert by_argv[("git", "log")].peak_cpu_cores == 0.5
    assert by_argv[("git", "log")].tool_call_id == "call_0_0"
    assert by_argv[("git", "log")].eligible is True
    assert by_argv[("git", "status")].eligible is False
    # call-level ineligible does not affect clause latency extraction.
    pytest_ev = next(ev for ev in events if ev.argv[0] == "python3")
    assert pytest_ev.eligible is False  # call.eligible_for_kb False


def test_parse_clause_artifact_rejects_invalid(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 2, "mode": "clause", "calls": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        parse_clause_artifact(path, "repo__x-1")


# ---------------------------------------------------------------------------
# Loader: trace
# ---------------------------------------------------------------------------


def test_parse_trace_extracts_tool_exec(tmp_path) -> None:
    lines = [
        {"type": "trace_metadata", "scaffold": "openclaw", "mode": "simulate"},
        {
            "type": "action",
            "action_type": "llm_call",
            "action_id": "llm_0",
            "ts_start": 1.0,
            "ts_end": 1.2,
            "data": {},
        },
        {
            "type": "action",
            "action_type": "tool_exec",
            "action_id": "tool_0_call_0_0",
            "ts_start": 1.3,
            "ts_end": 1.45,
            "iteration": 0,
            "data": {
                "tool_name": "exec",
                "tool_call_id": "call_0_0",
                "tool_args": json.dumps({"command": "cd /testbed && git log"}),
                "duration_ms": 150.0,
                "success": True,
            },
        },
        {
            "type": "action",
            "action_type": "tool_exec",
            "action_id": "tool_1_call_1_0",
            "ts_start": 1.5,
            "ts_end": 1.51,
            "iteration": 1,
            "data": {
                "tool_name": "read_file",
                "tool_call_id": "call_1_0",
                "tool_args": json.dumps({"path": "/testbed/src/x.py"}),
                "duration_ms": 8.0,
                "success": True,
            },
        },
    ]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    calls = parse_trace(path, "repo__x-1")
    assert len(calls) == 2
    exec_call = calls[0]
    assert exec_call.tool_name == "exec"
    assert exec_call.command == "cd /testbed && git log"
    assert exec_call.duration_ms == 150.0
    assert exec_call.success is True
    assert exec_call.tool_call_id == "call_0_0"
    read_call = calls[1]
    assert read_call.tool_name == "read_file"
    assert read_call.command is None  # read_file has no command field


def test_load_task_enriches_cpu_from_clause_events(tmp_path) -> None:
    task_dir = tmp_path / "psf__black-2037"
    attempt = task_dir / "attempt_1"
    attempt.mkdir(parents=True)
    artifact = _artifact(
        [
            _call(
                [
                    _clause("git", ["log"], 50.0, peak_cpu_cores=0.4),
                    _clause("git", ["show"], 60.0, peak_cpu_cores=0.9),
                ],
                tool_call_id="call_0_0",
                command="git log && git show",
            )
        ]
    )
    (attempt / "clause_telemetry.json").write_text(json.dumps(artifact), encoding="utf-8")
    trace_lines = [
        {
            "type": "action",
            "action_type": "tool_exec",
            "ts_start": 0.0,
            "ts_end": 0.06,
            "data": {
                "tool_name": "exec",
                "tool_call_id": "call_0_0",
                "tool_args": json.dumps({"command": "git log && git show"}),
                "duration_ms": 60.0,
                "success": True,
            },
        }
    ]
    (attempt / "trace.jsonl").write_text(
        "\n".join(json.dumps(line) for line in trace_lines), encoding="utf-8"
    )
    task = load_task(task_dir)
    assert task.task_id == "psf__black-2037"
    assert len(task.clause_events) == 2
    assert len(task.tool_calls) == 1
    assert task.tool_calls[0].peak_cpu_cores == 0.9  # max over clauses
    assert task.rejected_attempts == []


def test_is_task_dir() -> None:
    assert is_task_dir("psf__black-2037")
    assert is_task_dir("org__repo-with-dashes-42")
    assert not is_task_dir("throughput_summary.json")
    assert not is_task_dir("simulate_cloud_model_c2_20260726T005356962.jsonl")


# ---------------------------------------------------------------------------
# Algorithm input builders
# ---------------------------------------------------------------------------


def test_to_clause_observation_and_completed_call() -> None:
    obs = to_clause_observation(
        _clause_event("r1", "git", ["git", "log"], 123.0, tool_call_id="c0")
    )
    assert obs.bin == "git"
    assert obs.argv == ("git", "log")
    assert obs.latency_ms == 123.0
    assert obs.ts_end == 0.123

    call = to_completed_call(
        _tool_call("r1", duration_ms=250.0, peak_cpu_cores=1.5)
    )
    assert (call.ts_end - call.ts_start) * 1000.0 == 250.0
    assert call.peak_cpu_cores_eligible is True
    assert call.peak_memory_mb_eligible is False


def test_build_runtime_public_without_memory_anchor() -> None:
    calls = [
        CompletedCall(
            repo="r1",
            tool_name="exec",
            command="echo hi",
            ts_start=0.0,
            ts_end=0.1,
            censored=False,
            peak_cpu_cores=0.5,
            peak_cpu_cores_eligible=True,
            peak_memory_mb=None,
            peak_memory_mb_eligible=False,
            ambient_before_mb=None,
        ),
        CompletedCall(
            repo="r1",
            tool_name="exec",
            command="pytest tests -q",
            ts_start=0.0,
            ts_end=5.0,
            censored=False,
            peak_cpu_cores=3.0,
            peak_cpu_cores_eligible=True,
            peak_memory_mb=None,
            peak_memory_mb_eligible=False,
            ambient_before_mb=None,
        ),
    ]
    kb = build_runtime_public(calls)
    assert ("global", "") in kb._public["latency_ms"]  # type: ignore[attr-defined]
    assert ("global", "") in kb._public["peak_cpu_cores"]  # type: ignore[attr-defined]
    query = ToolCallQuery(
        repo="r2",
        tool_name="exec",
        command="echo hi",
        ts_start=1.0,
        ambient_before_mb=None,
    )
    predictions = kb.query(query)
    assert predictions["latency_ms"].conditional_p90 is not None
    assert predictions["peak_cpu_cores"].conditional_p90 is not None
    # Memory target is honestly unavailable without an ambient anchor.
    assert predictions["peak_memory_mb"].conditional_p90 is None
    assert predictions["peak_memory_mb"].note is not None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_summaries() -> None:
    bucket_records = [
        {"actual_bucket": 1, "predicted_bucket": 1, "probability_by_bucket": [0.1, 0.9, 0.0], "actual_ms": 150.0},
        {"actual_bucket": 1, "predicted_bucket": 2, "probability_by_bucket": [0.0, 0.0, 1.0], "actual_ms": 150.0},
        {"actual_bucket": 0, "predicted_bucket": None, "probability_by_bucket": None, "unavailable_reason": "no_evidence", "actual_ms": 20.0},
    ]
    bucket_summary = summarize_bucket(bucket_records)
    assert bucket_summary["n"] == 3
    assert bucket_summary["coverage"] == 2 / 3
    assert bucket_summary["accuracy"] == 0.5
    assert bucket_summary["brier_score"] is not None
    assert "no_evidence" in bucket_summary["unavailable_reasons"]

    point_records = [
        {"actual_ms": 100.0, "predicted_ms": 110.0},
        {"actual_ms": 200.0, "predicted_ms": 150.0},
        {"actual_ms": 300.0, "predicted_ms": None, "unavailable_reason": "no_evidence"},
    ]
    point_summary = summarize_point(point_records)
    assert point_summary["n"] == 3
    assert point_summary["coverage"] == 2 / 3
    assert point_summary["mae_ms"] == 30.0  # |100-110| + |200-150| = 80 / 2
    assert point_summary["median_abs_error_ms"] == 30.0

    quantile_records = [
        {"actual": 100.0, "predicted": 200.0},
        {"actual": 500.0, "predicted": 300.0},
        {"actual": None, "predicted": 300.0, "unavailable_reason": "no_actual"},
    ]
    quantile_summary = summarize_quantile(quantile_records)
    assert quantile_summary["n"] == 3
    assert quantile_summary["coverage"] == 2 / 3
    assert quantile_summary["pinball_q"] is not None


# ---------------------------------------------------------------------------
# End-to-end engine
# ---------------------------------------------------------------------------


def _synthetic_tasks(n_tasks: int = 6) -> dict[str, TaskArtifacts]:
    tasks: dict[str, TaskArtifacts] = {}
    for i in range(n_tasks):
        tid = f"repo{i}__pkg-{i}"
        clause_events = [
            _clause_event(tid, "git", ["git", "log"], 50.0 + i, eligible=True, tool_call_id="c0", peak_cpu_cores=0.5),
            _clause_event(tid, "python3", ["python3", "-m", "pytest"], 300.0 + i * 10, eligible=True, tool_call_id="c1", peak_cpu_cores=1.2),
            _clause_event(tid, "git", ["git", "status"], 20.0, eligible=False, tool_call_id="c2"),
        ]
        tool_calls = [
            _tool_call(tid, command="git log", duration_ms=55.0, ts_start=0.0, ts_end=0.055, tool_call_id="c0", peak_cpu_cores=0.5),
            _tool_call(tid, command="python3 -m pytest", duration_ms=310.0, ts_start=0.1, ts_end=0.41, tool_call_id="c1", peak_cpu_cores=1.2),
            _tool_call(tid, tool_name="read_file", command=None, duration_ms=15.0, ts_start=0.5, ts_end=0.515, tool_call_id="c2"),
        ]
        tasks[tid] = _task(tid, clause_events, tool_calls)
    return tasks


def test_evaluate_end_to_end() -> None:
    tasks = _synthetic_tasks(6)
    result = evaluate(
        tasks,
        dataset_dir="fixture",
        config=EvalConfig(train_frac=0.5, seed=1),
    )
    assert set(result.train_ids) | set(result.test_ids) == set(result.all_task_ids)
    assert set(result.train_ids).isdisjoint(result.test_ids)
    assert len(result.train_ids) == 3
    assert len(result.test_ids) == 3
    assert result.counts["train_tasks"] == 3
    assert result.counts["test_tasks"] == 3
    assert result.counts["train_clause_observations_eligible"] > 0
    # Every track must have recorded at least one sample on the test split.
    for track, records in result.records.items():
        assert records, f"track {track} has no records"
    # Bucket coverage should be 1.0 because the public prior has a global node.
    assert result.summaries["clause_latency_bucket"]["coverage"] == 1.0
    for track in LATTICE_TIME_ALGORITHMS:
        assert result.summaries[track]["n"] > 0
        assert result.summaries[track]["coverage"] == 1.0
    assert result.summaries["continuous_latency_p90"]["n"] > 0
    # Serialization round-trip.
    obj = result.to_json_obj()
    assert obj["train_ids"] == result.train_ids
    assert "clause_latency_bucket" in obj["summaries"]


def test_evaluate_empty_tasks() -> None:
    result = evaluate({}, dataset_dir="empty", config=EvalConfig())
    assert result.train_ids == []
    assert result.test_ids == []
    assert result.counts["train_tasks"] == 0
