from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_scheduler.predictors.tool_resource as tool_resource_predictor
from agent_scheduler.api.app import create_app
from agent_scheduler.api.dependencies import build_state
from agent_scheduler.config import SchedulerConfig
from agent_scheduler.contracts.models import ParamFeatures, ToolBeforeRequest, ToolCompletedEvent
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from agent_scheduler.predictors.tool_resource import (
    ToolResourcePredictor,
    load_openclaw_trace_observations,
)
from tool_resource.runtime_kb import LatencyBuckets
import tool_resource.runtime_kb as tool_resource_runtime_kb


def _test_parse_command(command: str) -> dict:
    clauses = []
    for part in command.split("&&"):
        argv = shlex.split(part.strip(), posix=True)
        if not argv:
            continue
        clauses.append({"bin": Path(argv[0]).name, "argv": argv})
    return {"clauses": clauses, "parse_failed": not clauses}


@pytest.fixture(autouse=True)
def _native_parser_fixture(monkeypatch) -> None:
    monkeypatch.setattr(tool_resource_predictor, "parse_command_clauses", _test_parse_command)
    monkeypatch.setattr(tool_resource_runtime_kb, "parse_command_clauses", _test_parse_command)


def _write_trace(path: Path, *, command: str = "python -m pytest tests -q") -> None:
    records = [
        {
            "schema_version": 6,
            "record_type": "trace_metadata",
            "trace_format_version": 6,
            "scaffold": "openclaw",
            "mode": "collect",
            "created_at": "2026-07-24T17:29:43.615462Z",
        },
        {
            "schema_version": 6,
            "record_type": "span_start",
            "trace_id": "run-1",
            "span_id": "call-1",
            "parent_span_id": None,
            "session_id": "session-1",
            "run_id": "run-1",
            "agent_id": "main",
            "sequence_no": 1,
            "kind": "tool",
            "name": "exec",
            "wall_time_ns": "1000000000000",
            "monotonic_time_ns": "1",
            "input": {"requested_args": {"command": command}},
            "execution": {"mode": "launcher", "execution_id": "call-1"},
        },
        {
            "schema_version": 6,
            "record_type": "span_end",
            "trace_id": "run-1",
            "span_id": "call-1",
            "parent_span_id": None,
            "session_id": "session-1",
            "run_id": "run-1",
            "agent_id": "main",
            "sequence_no": 1,
            "kind": "tool",
            "name": "exec",
            "wall_time_ns": "1001200000000",
            "monotonic_time_ns": "2",
            "duration_ns": "1200000000",
            "status": {"code": "ok", "message": None},
            "output": {"exit_code": 0, "result": None},
            "execution": {"mode": "launcher", "execution_id": "call-1"},
            "resources": {
                "cpu_utilization_avg_cores": 1.5,
                "rss_peak_bytes": 104857600,
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )


def test_openclaw_trace_v6_loads_as_tool_resource_observations(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace)

    loaded = load_openclaw_trace_observations(trace, repo="repo-1")

    assert loaded.tool_spans_seen == 1
    assert len(loaded.observations) == 1
    observation = loaded.observations[0]
    assert observation.repo == "repo-1"
    assert observation.bin == "python"
    assert observation.argv == ("python", "-m", "pytest", "tests", "-q")
    assert observation.latency_ms == 1200
    assert observation.sampled_peak_rss_mb == 100


def test_tool_resource_predictor_predicts_from_openclaw_trace(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace)
    predictor = ToolResourcePredictor.from_openclaw_traces(
        [trace],
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    request = ToolBeforeRequest(
        schema_version="scheduler.v1",
        event_id="evt-1",
        occurred_at="2026-07-24T17:29:44Z",
        plugin_version="0.1.0",
        run_id="run-2",
        session_id="session-1",
        session_key=None,
        agent_id="main",
        tool_call_id="call-2",
        tool_name="exec",
        tool_kind="shell",
        tool_input_kind="json",
        derived_paths=[],
        params_digest="sha256:" + "a" * 64,
        param_features=ParamFeatures(
            serialized_size_bytes=10,
            string_length=10,
            list_item_count=0,
            path_count=0,
            has_command_like_field=True,
        ),
        raw_params={"command": "python -m pytest tests -q"},
    )

    result = asyncio.run(predictor.predict(request))

    assert result.resource_class == "latency_medium"
    assert result.duration_p50_ms == 1250
    assert result.confidence == 1.0
    continuous = result.tool_resource["continuous_predictions"]
    without_continuous = result.tool_resource | {"continuous_predictions": {}}
    assert without_continuous == {
        "repo": "repo-1",
        "command": "python -m pytest tests -q",
        "parse_failed": False,
        "clause_bins": ["python"],
        "prediction": {
            "bucket_id": 2,
            "probability_by_bucket": [0.0, 0.0, 1.0, 0.0],
            "scope": "repo",
            "key_kind": "exact_clause",
            "evidence_count": 1,
            "fallback_path": ["repo:exact_clause"],
        },
        "unavailable_reason": None,
        "continuous_predictions": {},
        "prediction_algorithms": _prediction_algorithms(),
    }
    assert continuous["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)
    assert continuous["latency_ms"]["scope"] == "repo"
    assert continuous["latency_ms"]["key_kind"] == "exact_command"
    assert continuous["peak_cpu_cores"]["conditional_p90"] == 1.5
    assert continuous["peak_cpu_cores"]["scope"] == "repo"
    assert continuous["peak_cpu_cores"]["key_kind"] == "exact_command"
    assert continuous["peak_memory_mb"] == {
        "target": "peak_memory_mb",
        "conditional_p90": None,
        "scope": None,
        "key_kind": None,
        "evidence_count": 0,
        "fallback_path": [],
        "note": "memory prediction requires ambient_before_mb anchor",
    }


def test_stage2_clause_identity_matches_online_prediction(tmp_path: Path, monkeypatch) -> None:
    def parse_command(command: str) -> dict:
        if command == "python -m pytest tests -q":
            return {
                "clauses": [{"bin": "python", "argv": ["python", "-m", "pytest", "tests", "-q"]}],
                "parse_failed": False,
            }
        return {
            "clauses": [{"bin": "git", "argv": ["git", "status"]}],
            "parse_failed": False,
        }

    monkeypatch.setattr(tool_resource_predictor, "parse_command_clauses", parse_command)
    stage2 = tmp_path / "stage2.json"
    stage2.write_text(
        json.dumps(
            {
                "version": 2,
                "mode": "clause",
                "replay_execution": "completed",
                "cleanup": "ok",
                "status_model": "call_granular_v1",
                "telemetry_quality": "ok",
                "collection_validity": "valid",
                "formal_completeness": "complete",
                "integrity": {"status": "ok"},
                "provenance": {"repo": "repo-1"},
                "calls": [
                    {
                        "eligible_for_kb": True,
                        "clauses": [
                            {
                                "availability": {"latency": "ok"},
                                "bin": "python",
                                "argv": ["python", "-m", "pytest", "tests", "-q"],
                                "latency_ms": 1200,
                                "ts_start": 1.0,
                                "ts_end": 2.2,
                            }
                        ],
                    },
                    {
                        "eligible_for_kb": True,
                        "clauses": [
                            {
                                "availability": {"latency": "ok"},
                                "bin": "git",
                                "argv": ["git", "status"],
                                "latency_ms": 50,
                                "ts_start": 3.0,
                                "ts_end": 3.05,
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(stage2,),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )

    result = asyncio.run(
        predictor.predict(_tool_request("evt-1", "call-1", "python -m pytest tests -q"))
    )

    assert predictor.report.observations_loaded == 2
    assert result.resource_class == "latency_medium"
    assert result.duration_p50_ms == 1250
    assert result.confidence == 1.0
    assert result.tool_resource is not None
    assert result.tool_resource["prediction"]["scope"] == "repo"
    assert result.tool_resource["prediction"]["key_kind"] == "exact_clause"
    assert result.tool_resource["prediction"]["evidence_count"] == 1
    assert result.tool_resource["continuous_predictions"]["peak_cpu_cores"][
        "conditional_p90"
    ] is None


def test_openclaw_trace_history_populates_repo_argv_prefix(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, command="python -m pytest tests -q")
    predictor = ToolResourcePredictor.from_openclaw_traces(
        [trace],
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )

    result = asyncio.run(
        predictor.predict(_tool_request("evt-prefix", "call-prefix", "python -m pytest integration -q"))
    )

    assert result.resource_class == "latency_medium"
    assert result.tool_resource["prediction"]["scope"] == "repo"
    assert result.tool_resource["prediction"]["key_kind"] == "argv_prefix_depth_3"
    assert result.tool_resource["prediction"]["fallback_path"] == [
        "repo:exact_clause",
        "repo:argv_prefix_depth_4",
        "repo:argv_prefix_depth_3",
    ]


def test_openclaw_trace_cold_start_persists_clause_kb(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    artifact_dir = tmp_path / "tool-resource"
    _write_trace(trace, command="python -m pytest tests -q")

    ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(trace,),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )

    snapshot = artifact_dir / "clause-resource-kb.json"
    assert snapshot.is_file()

    reloaded = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )
    result = asyncio.run(
        reloaded.predict(_tool_request("evt-prefix", "call-prefix", "python -m pytest integration -q"))
    )

    assert result.tool_resource["prediction"]["scope"] == "repo"
    assert result.tool_resource["prediction"]["key_kind"] == "argv_prefix_depth_3"


def test_stage2_loader_uses_native_sdk_artifact_validation(tmp_path: Path) -> None:
    stage2 = tmp_path / "invalid-stage2.json"
    stage2.write_text(
        json.dumps(
            {
                "version": 2,
                "mode": "clause",
                "provenance": {"repo": "repo-1"},
                "calls": [
                    {
                        "eligible_for_kb": True,
                        "clauses": [
                            {
                                "bin": "python",
                                "argv": ["python", "-m", "pytest"],
                                "latency_ms": 1200,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(stage2,),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )

    assert predictor.report.stage2_traces_loaded == 0
    assert predictor.report.observations_loaded == 0
    assert predictor.report.kb_available is False
    assert predictor.report.rejections


def test_tool_resource_predictor_exposes_native_unavailable_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def parse_command(_command: str) -> dict:
        return {
            "clauses": [
                {"bin": "python", "argv": ["python", "-m", "pytest"]},
                {"bin": "git", "argv": ["git", "status"]},
            ],
            "parse_failed": False,
        }

    monkeypatch.setattr(tool_resource_predictor, "parse_command_clauses", parse_command)
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, command="python -m pytest")
    predictor = ToolResourcePredictor.from_openclaw_traces(
        [trace],
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )

    result = asyncio.run(
        predictor.predict(_tool_request("evt-1", "call-1", "python -m pytest && git status"))
    )

    assert result.resource_class == "unknown"
    continuous = result.tool_resource["continuous_predictions"]
    without_continuous = result.tool_resource | {"continuous_predictions": {}}
    assert without_continuous == {
        "repo": "repo-1",
        "command": "python -m pytest && git status",
        "parse_failed": False,
        "clause_bins": ["python", "git"],
        "prediction": None,
        "unavailable_reason": "compound_command_uncomposed",
        "continuous_predictions": {},
        "prediction_algorithms": _prediction_algorithms(),
    }
    assert continuous["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)
    assert continuous["latency_ms"]["key_kind"] == "command_prefix_depth_3"
    assert continuous["peak_cpu_cores"]["conditional_p90"] == 1.5
    assert continuous["peak_cpu_cores"]["key_kind"] == "command_prefix_depth_3"
    assert continuous["peak_memory_mb"]["note"] == "memory prediction requires ambient_before_mb anchor"


def test_tool_resource_predictor_learns_from_completion_without_cold_start() -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    request = _tool_request("evt-1", "call-1", "python -m pytest tests -q")
    predictor.record_tool_started(request)

    added = predictor.observe_completion(
        ToolCompletedEvent(
            schema_version="scheduler.v1",
            event_id="evt-1",
            occurred_at="2026-07-24T17:29:45Z",
            plugin_version="0.1.0",
            run_id="run-1",
            session_id="session-1",
            session_key=None,
            agent_id="main",
            tool_call_id="call-1",
            decision_id=None,
            lease_id=None,
            execution_id=None,
            tool_name="exec",
            duration_ms=1200,
            succeeded=True,
            error_type=None,
            error_digest=None,
            result_size_bytes=None,
            raw_result=None,
            raw_event=None,
            resource_scope=None,
        ),
        _runtime_sample("evt-1", "call-1"),
    )
    result = asyncio.run(
        predictor.predict(_tool_request("evt-2", "call-2", "python -m pytest tests -q"))
    )

    assert added == 1
    assert result.resource_class == "latency_medium"
    assert result.duration_p50_ms == 1250
    assert result.tool_resource["continuous_predictions"]["peak_cpu_cores"][
        "conditional_p90"
    ] == 0.8
    assert result.tool_resource["continuous_predictions"]["peak_memory_mb"][
        "note"
    ] == "memory prediction requires ambient_before_mb anchor"


def test_tool_resource_predictor_persists_clause_kb_prefixes(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "tool-resource"
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )
    request = _tool_request("evt-1", "call-1", "python -m pytest tests -q")
    predictor.record_tool_started(request)

    assert predictor.observe_completion(
        ToolCompletedEvent(
            schema_version="scheduler.v1",
            event_id="evt-1",
            occurred_at="2026-07-24T17:29:45Z",
            plugin_version="0.1.0",
            run_id="run-1",
            session_id="session-1",
            session_key=None,
            agent_id="main",
            tool_call_id="call-1",
            decision_id=None,
            lease_id=None,
            execution_id=None,
            tool_name="exec",
            duration_ms=1200,
            succeeded=True,
            error_type=None,
            error_digest=None,
            result_size_bytes=None,
            raw_result=None,
            raw_event=None,
            resource_scope=None,
        ),
        _runtime_sample("evt-1", "call-1"),
    ) == 1
    assert (artifact_dir / "clause-resource-kb.json").is_file()

    reloaded = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )
    result = asyncio.run(
        reloaded.predict(_tool_request("evt-2", "call-2", "python -m pytest integration -q"))
    )

    assert result.resource_class == "latency_medium"
    assert result.tool_resource["prediction"]["scope"] == "repo"
    assert result.tool_resource["prediction"]["key_kind"] == "argv_prefix_depth_3"


def test_tool_resource_predictor_continuous_memory_uses_ambient_anchor() -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    request = _tool_request("evt-1", "call-1", "python -m pytest tests -q")
    predictor.record_tool_started(request)
    assert predictor.observe_completion(
        ToolCompletedEvent(
            schema_version="scheduler.v1",
            event_id="evt-1",
            occurred_at="2026-07-24T17:29:45Z",
            plugin_version="0.1.0",
            run_id="run-1",
            session_id="session-1",
            session_key=None,
            agent_id="main",
            tool_call_id="call-1",
            decision_id=None,
            lease_id=None,
            execution_id=None,
            tool_name="exec",
            duration_ms=1200,
            succeeded=True,
            error_type=None,
            error_digest=None,
            result_size_bytes=None,
            raw_result=None,
            raw_event=None,
            resource_scope=None,
        ),
        _runtime_sample("evt-1", "call-1"),
    ) == 1

    result = asyncio.run(
        predictor.predict(
            _tool_request("evt-2", "call-2", "python -m pytest tests -q"),
            ambient_before_mb=50.0,
        )
    )

    memory = result.tool_resource["continuous_predictions"]["peak_memory_mb"]
    assert memory["conditional_p90"] == pytest.approx(150.0)
    assert memory["scope"] == "repo"
    assert memory["key_kind"] == "exact_command"
    assert memory["note"] == "residual quantile plus query ambient_before_mb"


def test_sidecar_uses_tool_resource_predictor_when_configured(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace)
    state = build_state(
        SchedulerConfig(
            tool_resource_trace_paths=(trace,),
            tool_resource_latency_buckets_ms=(100.0, 500.0, 2_000.0),
        )
    )
    client = TestClient(create_app(state))

    response = client.post(
        "/v1/decisions/tool",
        json={
            "schema_version": "scheduler.v1",
            "event_id": "evt-1",
            "occurred_at": "2026-07-24T17:29:44Z",
            "plugin_version": "0.1.0",
            "run_id": "run-2",
            "session_id": "session-1",
            "session_key": None,
            "agent_id": "main",
            "tool_call_id": "call-2",
            "tool_name": "exec",
            "tool_kind": "shell",
            "tool_input_kind": "json",
            "operation_hint": None,
            "derived_paths": [],
            "params_digest": "sha256:" + "a" * 64,
            "param_features": {
                "serialized_size_bytes": 10,
                "string_length": 10,
                "list_item_count": 0,
                "path_count": 0,
                "has_command_like_field": True,
            },
            "raw_params": {"command": "python -m pytest tests -q"},
            "resource_scope": None,
        },
    )

    assert response.status_code == 200
    assert response.json()["prediction"]["resource_class"] == "latency_medium"


def test_trace_writes_tool_prediction_payload(tmp_path: Path) -> None:
    source_trace = tmp_path / "source-trace.jsonl"
    trace_dir = tmp_path / "written-traces"
    _write_trace(source_trace)
    state = build_state(
        SchedulerConfig(
            trace_dir=trace_dir,
            tool_resource_trace_paths=(source_trace,),
            tool_resource_latency_buckets_ms=(100.0, 500.0, 2_000.0),
        )
    )
    client = TestClient(create_app(state))
    request = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-before",
        "occurred_at": "2026-07-24T17:29:44Z",
        "plugin_version": "0.1.0",
        "run_id": "run-trace-prediction",
        "session_id": "session-trace-prediction",
        "session_key": None,
        "agent_id": "main",
        "tool_call_id": "call-prediction",
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": None,
        "derived_paths": [],
        "params_digest": "sha256:" + "a" * 64,
        "param_features": {
            "serialized_size_bytes": 10,
            "string_length": 10,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": {"command": "python -m pytest tests -q"},
        "resource_scope": None,
    }
    decision = client.post("/v1/decisions/tool", json=request).json()
    assert decision["prediction"]["resource_class"] == "latency_medium"

    completion = {
        "schema_version": "scheduler.v1",
        "event_id": "evt-after",
        "occurred_at": "2026-07-24T17:29:45Z",
        "plugin_version": "0.1.0",
        "run_id": "run-trace-prediction",
        "session_id": "session-trace-prediction",
        "session_key": None,
        "agent_id": "main",
        "tool_call_id": "call-prediction",
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "exec",
        "duration_ms": 1200,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": None,
        "raw_result": None,
        "resource_scope": None,
    }
    assert client.post("/v1/events/tool-completed", json=completion).json() == {"stored": True}

    records = [
        json.loads(line)
        for path in trace_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    tool_start = next(
        record
        for record in records
        if record.get("record_type") == "span_start" and record.get("kind") == "tool"
    )
    assert tool_start["prediction"] == decision["prediction"]
    assert tool_start["prediction"]["tool_resource"]["prediction"]["bucket_id"] == 2
    algorithms = tool_start["prediction"]["tool_resource"]["prediction_algorithms"]
    assert [item["name"] for item in algorithms["enabled"]] == [
        "clause_latency_bucket",
        "runtime_tool_resource_conditional_p90",
    ]
    assert algorithms["excluded"] == [
        {
            "name": "quantile_mlp",
            "source": "tool_resource.mlp",
            "reason": "not enabled by the sidecar; this integration uses non-MLP empirical predictors only",
        }
    ]


def _tool_request(event_id: str, tool_call_id: str, command: str) -> ToolBeforeRequest:
    return ToolBeforeRequest(
        schema_version="scheduler.v1",
        event_id=event_id,
        occurred_at="2026-07-24T17:29:44Z",
        plugin_version="0.1.0",
        run_id="run-1",
        session_id="session-1",
        session_key=None,
        agent_id="main",
        tool_call_id=tool_call_id,
        tool_name="exec",
        tool_kind="shell",
        tool_input_kind="json",
        derived_paths=[],
        params_digest="sha256:" + "a" * 64,
        param_features=ParamFeatures(
            serialized_size_bytes=10,
            string_length=10,
            list_item_count=0,
            path_count=0,
            has_command_like_field=True,
        ),
        raw_params={"command": command},
    )


def _runtime_sample(event_id: str, tool_call_id: str) -> ToolRuntimeSample:
    return ToolRuntimeSample(
        event_id=event_id,
        tool_call_id=tool_call_id,
        tool_name="exec",
        operation="pytest",
        started_at=1_000.0,
        ended_at=1_001.2,
        duration_ms=1200,
        monitor_duration_ms=1200,
        monitor_start_wall_s=1_000.0,
        monitor_end_wall_s=1_001.2,
        monitor_start_monotonic_s=10.0,
        monitor_end_monotonic_s=11.2,
        cpu_time_delta_s=1.0,
        rss_bytes_before=10,
        rss_bytes_after=20,
        read_bytes_delta=0,
        write_bytes_delta=0,
        net_rx_bytes_delta=0,
        net_tx_bytes_delta=0,
        ctx_switches_delta=0,
        rss_bytes_peak=104857600,
        cpu_utilization_avg_cores=0.8,
        cpu_utilization_avg_pct=80.0,
        disk_read_bytes_per_s=0.0,
        disk_write_bytes_per_s=0.0,
        net_rx_bytes_per_s=0.0,
        net_tx_bytes_per_s=0.0,
        sampling_interval_ms=50,
        sampling_point_count=2,
        sampling_quality="ok",
        resource_timeline=[],
        resource_timeline_truncated=False,
        resource_class="unknown",
        target_pid=123,
        process_count_before=1,
        process_count_after=1,
        attribution_status="pid",
        monitor_source="pid",
    )


def _prediction_algorithms() -> dict:
    return {
        "enabled": [
            {
                "name": "clause_latency_bucket",
                "family": "empirical_bucket",
                "source": "ClauseResourceKB",
                "targets": ["latency_ms"],
                "outputs": [
                    "bucket_id",
                    "probability_by_bucket",
                    "duration_p50_ms",
                    "duration_p90_ms",
                    "resource_class",
                ],
            },
            {
                "name": "runtime_tool_resource_conditional_p90",
                "family": "empirical_ecdf",
                "source": "RuntimeToolResourceKB",
                "targets": ["latency_ms", "peak_cpu_cores", "peak_memory_mb"],
                "outputs": ["conditional_p90"],
            },
        ],
        "excluded": [
            {
                "name": "quantile_mlp",
                "source": "tool_resource.mlp",
                "reason": "not enabled by the sidecar; this integration uses non-MLP empirical predictors only",
            }
        ],
    }
