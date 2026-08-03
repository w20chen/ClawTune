from __future__ import annotations

import asyncio
import concurrent.futures
import json
import shlex
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

import agent_scheduler.predictors.tool_resource as tool_resource_predictor
from agent_scheduler.api.app import create_app
from agent_scheduler.api.dependencies import build_state
from agent_scheduler.config import SchedulerConfig
from agent_scheduler.contracts.models import (
    ParamFeatures,
    ResourceScope,
    ToolBeforeRequest,
    ToolCompletedEvent,
)
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from agent_scheduler.predictors.tool_resource import (
    ToolResourcePredictor,
    load_openclaw_trace_observations,
)
from tool_resource.runtime_kb import (
    ClauseObservation,
    ClauseResourceKB,
    CompletedCall,
    LatencyBuckets,
    RuntimeToolResourceKB,
    ToolCallQuery,
)
import tool_resource.runtime_kb as tool_resource_runtime_kb


def _test_parse_command(command: str) -> dict:
    clauses = []
    for part in command.split("&&"):
        argv = shlex.split(part.strip(), posix=True)
        if not argv:
            continue
        clauses.append({"bin": Path(argv[0]).name, "argv": argv})
    return {"clauses": clauses, "parse_failed": not clauses}


def test_stage2_workload_result_preserves_masked_lookup_diagnostic() -> None:
    workload = tool_resource_predictor._stage2_workload_result(
        {
            "content": [{"type": "text", "text": "/bin/sh: 1: pip: not found"}],
            "details": {
                "aggregated": "/bin/sh: 1: pip: not found",
                "exitCode": 0,
            },
        },
        exit_code=0,
        signal=None,
        succeeded=True,
    )

    assert workload == {
        "exit_code": 0,
        "signal": None,
        "ok": True,
        "result": "/bin/sh: 1: pip: not found",
        "stderr": "",
    }


class _FakeToolResourceSDK:
    def __init__(self) -> None:
        self.started: list[tuple[str | None, str, str]] = []
        self.contexts: list[object] = []

    def start_command(self, context, tool_call_id: str, command: str):
        self.contexts.append(context)
        self.started.append((context.container_id, tool_call_id, command))
        return SimpleNamespace(
            tool_call_id=tool_call_id,
            _observer=SimpleNamespace(
                telemetry_available=True,
                unavailable_reason=None,
            ),
        )


@pytest.fixture(autouse=True)
def _native_parser_fixture(monkeypatch) -> None:
    monkeypatch.setattr(tool_resource_predictor, "parse_command_clauses", _test_parse_command)
    monkeypatch.setattr(tool_resource_runtime_kb, "parse_command_clauses", _test_parse_command)


def _write_trace(
    path: Path,
    *,
    command: str = "python -m pytest tests -q",
    memory_rss_bytes_before: int | None = None,
    attribution_source: str | None = None,
) -> None:
    resources = {
        "cpu_utilization_avg_cores": 1.5,
        "rss_peak_bytes": 104857600,
    }
    if memory_rss_bytes_before is not None:
        resources["memory_rss_bytes_before"] = memory_rss_bytes_before
    if attribution_source is not None:
        resources["attribution_source"] = attribution_source
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
            "resources": resources,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )


def test_predictor_retries_stage2_after_container_id_arrives(tmp_path: Path) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0)),
        repo="repo-1",
        artifact_dir=tmp_path / "tool-resource",
    )
    fake_sdk = _FakeToolResourceSDK()
    predictor._sdk = fake_sdk  # type: ignore[assignment]

    assert not predictor.begin_execution(
        execution_id="exec-1",
        tool_call_id="call-1",
        command="echo hi && true",
        container_id=None,
        repo="repo-1",
    )
    assert predictor.begin_execution(
        execution_id="exec-1",
        tool_call_id="call-1",
        command="echo hi && true",
        container_id="a" * 64,
        repo="repo-1",
    )

    assert fake_sdk.started == [("a" * 64, "call-1", "echo hi && true")]


def test_predictor_starts_host_ebpf_from_trusted_pid_and_cgroup(
    tmp_path: Path,
) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0)),
        repo="repo-1",
        artifact_dir=tmp_path / "tool-resource",
    )
    fake_sdk = _FakeToolResourceSDK()
    predictor._sdk = fake_sdk  # type: ignore[assignment]
    cgroup = tmp_path / "host-cgroup"
    cgroup.mkdir()

    assert predictor.begin_execution(
        execution_id="exec-host",
        tool_call_id="call-host",
        command="python -c 'print(1)'",
        container_id=None,
        cgroup_path=str(cgroup),
        trusted_root_pid=4242,
        repo="repo-1",
    )

    assert fake_sdk.started == [(None, "call-host", "python -c 'print(1)'")]
    context = fake_sdk.contexts[0]
    assert context.cgroup_path == str(cgroup)
    assert context.trusted_root_pid == 4242


def test_predictor_passes_and_rebinds_trusted_execution_root(tmp_path: Path) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0)),
        repo="repo-1",
        artifact_dir=tmp_path / "tool-resource",
    )
    bound_roots: list[int] = []

    class SDK:
        def start_command(self, context, tool_call_id: str, command: str):
            assert context.trusted_root_pid == 4242
            return SimpleNamespace(
                tool_call_id=tool_call_id,
                _observer=SimpleNamespace(
                    telemetry_available=True,
                    unavailable_reason=None,
                    bind_trusted_root=bound_roots.append,
                ),
            )

    predictor._sdk = SDK()  # type: ignore[assignment]

    assert predictor.begin_execution(
        execution_id="exec-1",
        tool_call_id="call-1",
        command="echo hi",
        container_id="a" * 64,
        trusted_root_pid=4242,
        gateway_id="gateway-a",
        runtime_id="runtime-a",
    )
    assert predictor.begin_execution(
        execution_id="exec-1",
        tool_call_id="call-1",
        command="echo hi",
        container_id="a" * 64,
        trusted_root_pid=4242,
        gateway_id="gateway-a",
        runtime_id="runtime-a",
    )
    assert bound_roots == [4242]
    assert predictor.active_execution_ids("runtime-a") == ("exec-1",)
    assert predictor.active_execution_ids("runtime-a", "gateway-a") == ("exec-1",)
    assert predictor.active_execution_ids("runtime-a", "gateway-b") == ()
    assert predictor.active_execution_ids("runtime-b") == ()

    with pytest.raises(ValueError, match="active execution owner changed"):
        predictor.begin_execution(
            execution_id="exec-1",
            tool_call_id="call-1",
            command="echo hi",
            container_id="a" * 64,
            trusted_root_pid=4242,
            gateway_id="gateway-b",
            runtime_id="runtime-a",
        )


def test_concurrent_duplicate_execution_start_attaches_only_once(tmp_path: Path) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0)),
        repo="repo-1",
        artifact_dir=tmp_path / "tool-resource",
    )
    starts = 0
    starts_lock = threading.Lock()

    class SDK:
        def start_command(self, _context, tool_call_id: str, _command: str):
            nonlocal starts
            with starts_lock:
                starts += 1
            time.sleep(0.03)
            return SimpleNamespace(
                tool_call_id=tool_call_id,
                _observer=SimpleNamespace(
                    telemetry_available=True,
                    unavailable_reason=None,
                ),
            )

    predictor._sdk = SDK()  # type: ignore[assignment]

    def begin() -> bool:
        return predictor.begin_execution(
            execution_id="same-execution",
            tool_call_id="same-call",
            command="printf ok",
            container_id="a" * 64,
            gateway_id="gateway",
            runtime_id="runtime",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        assert all(executor.map(lambda _index: begin(), range(64)))
    assert starts == 1


def test_sanitized_execution_ids_cannot_collide_artifact_paths(tmp_path: Path) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0)),
        repo="repo-1",
        artifact_dir=tmp_path / "tool-resource",
    )
    fake_sdk = _FakeToolResourceSDK()
    predictor._sdk = fake_sdk  # type: ignore[assignment]

    for execution_id in ("exec/a", "exec_a"):
        assert predictor.begin_execution(
            execution_id=execution_id,
            tool_call_id=execution_id,
            command="printf ok",
            container_id="a" * 64,
        )

    paths = {context.artifact_path.name for context in fake_sdk.contexts}
    assert len(paths) == 2


def test_predictor_does_not_report_fail_isolated_collector_as_started(
    tmp_path: Path,
) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0)),
        repo="repo-1",
        artifact_dir=tmp_path / "tool-resource",
    )

    class UnavailableSDK:
        def start_command(self, context, tool_call_id: str, command: str):
            return SimpleNamespace(
                tool_call_id=tool_call_id,
                _observer=SimpleNamespace(
                    telemetry_available=False,
                    unavailable_reason="collector attach failed: permission denied",
                ),
            )

    predictor._sdk = UnavailableSDK()  # type: ignore[assignment]

    assert not predictor.begin_execution(
        execution_id="exec-1",
        tool_call_id="call-1",
        command="echo hi",
        container_id="a" * 64,
    )
    summary = predictor._telemetry_by_execution_id["exec-1"]
    assert summary.started is True
    assert summary.status == "unavailable"
    assert summary.unavailable_reason == "collector attach failed: permission denied"


def test_openclaw_trace_v6_loads_as_tool_resource_observations(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace)

    loaded = load_openclaw_trace_observations(trace, repo="repo-1")

    assert loaded.tool_spans_seen == 1
    assert loaded.observations == ()
    assert len(loaded.completed_calls) == 1
    completed = loaded.completed_calls[0]
    assert completed.repo == "repo-1"
    assert completed.tool_name == "exec"
    assert completed.command == "python -m pytest tests -q"
    assert completed.ts_end - completed.ts_start == pytest.approx(1.2)
    assert completed.peak_memory_mb == 100


def test_openclaw_trace_span_repo_overrides_batch_fallback(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace)
    records = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    for record in records:
        if record.get("kind") == "tool":
            record["repo"] = "owner/project"
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    loaded = load_openclaw_trace_observations(
        trace,
        repo="swe-rebench-batch",
    )
    assert len(loaded.completed_calls) == 1
    assert loaded.completed_calls[0].repo == "owner/project"

    predictor = ToolResourcePredictor.from_openclaw_traces(
        [trace],
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="swe-rebench-batch",
    )
    owner_request = _tool_request(
        "evt-owner",
        "call-owner",
        "python -m pytest tests -q",
    ).model_copy(update={"repo": "owner/project"})
    batch_request = _tool_request(
        "evt-batch",
        "call-batch",
        "python -m pytest tests -q",
    ).model_copy(update={"repo": "swe-rebench-batch"})

    owner = asyncio.run(predictor.predict(owner_request))
    batch = asyncio.run(predictor.predict(batch_request))

    assert owner.tool_resource["continuous_predictions"]["latency_ms"][
        "scope"
    ] == "repo"
    assert batch.tool_resource["continuous_predictions"]["latency_ms"][
        "scope"
    ] is None


def test_openclaw_trace_rejects_mismatched_span_repos(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace)
    records = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    for record in records:
        if record.get("record_type") == "span_start":
            record["repo"] = "owner/project-a"
        elif record.get("record_type") == "span_end":
            record["repo"] = "owner/project-b"
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"tool span 'call-1' repo mismatch"):
        load_openclaw_trace_observations(trace, repo="swe-rebench-batch")

    predictor = ToolResourcePredictor.from_openclaw_traces(
        [trace],
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="swe-rebench-batch",
    )
    assert predictor.report.openclaw_traces_accepted == 0
    assert predictor.report.continuous_observations_loaded == 0
    assert any("repo mismatch" in rejection for rejection in predictor.report.rejections)


@pytest.mark.parametrize(
    "attribution_source",
    ["shared-sandbox-container", "shared-runtime-process"],
)
def test_openclaw_trace_shared_scope_keeps_only_runtime_latency(
    tmp_path: Path,
    attribution_source: str,
) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        memory_rss_bytes_before=50 * 1024 * 1024,
        attribution_source=attribution_source,
    )

    loaded = load_openclaw_trace_observations(trace, repo="repo-1")

    assert len(loaded.completed_calls) == 1
    completed = loaded.completed_calls[0]
    assert completed.peak_cpu_cores == pytest.approx(1.5)
    assert completed.peak_cpu_cores_eligible is False
    assert completed.peak_memory_mb == pytest.approx(100.0)
    assert completed.peak_memory_mb_eligible is False

    records = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    start = next(row for row in records if row["record_type"] == "span_start")
    end = next(row for row in records if row["record_type"] == "span_end")
    clause = tool_resource_predictor._observation_from_tool_span(
        start,
        end,
        repo="repo-1",
    )
    assert clause is not None
    assert clause.latency_ms == pytest.approx(1200.0)
    assert clause.peak_cpu_cores is None
    assert clause.sampled_peak_rss_mb is None
    assert clause.cpu_ns_cumulative is None

    predictor = ToolResourcePredictor.from_openclaw_traces(
        [trace],
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    prediction = asyncio.run(
        predictor.predict(
            _tool_request("evt-next", "call-next", "python -m pytest tests -q"),
            ambient_before_mb=50.0,
        )
    )
    continuous = prediction.tool_resource["continuous_predictions"]
    assert continuous["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)
    assert continuous["peak_cpu_cores"]["conditional_p90"] is None
    assert continuous["peak_memory_mb"]["conditional_p90"] is None


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
    assert result.duration_p50_ms == 1200
    assert result.duration_p90_ms == 1200
    assert result.confidence is None
    continuous = result.tool_resource["continuous_predictions"]
    without_continuous = result.tool_resource | {"continuous_predictions": {}}
    assert without_continuous == {
        "repo": "repo-1",
        "command": "python -m pytest tests -q",
        "parse_failed": False,
        "clause_bins": ["python"],
        "clause_predictions": [
            {
                "clause_index": 0,
                "bin": "python",
                "argv": ["python", "-m", "pytest", "tests", "-q"],
                "prediction": None,
                "unavailable_reason": "no_clause_latency_evidence",
            }
        ],
        "prediction": None,
        "unavailable_reason": "no_clause_latency_evidence",
        "continuous_predictions": {},
        "lattice_time_predictions": _unavailable_lattice_time_predictions(
            [("python", ["python", "-m", "pytest", "tests", "-q"])]
        ),
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
    assert result.tool_resource["clause_predictions"] == [
        {
            "clause_index": 0,
            "bin": "python",
            "argv": ["python", "-m", "pytest", "tests", "-q"],
            "prediction": result.tool_resource["prediction"],
            "unavailable_reason": None,
        }
    ]
    lattice = result.tool_resource["lattice_time_predictions"]
    assert predictor.report.lattice_observations_loaded == 2
    assert predictor.report.lattice_kb_available is True
    assert len(lattice) == 1
    assert lattice[0]["clause_index"] == 0
    assert [item["algorithm"] for item in lattice[0]["predictions"]] == [
        "shrinkage",
        "loso",
        "max_cardinality",
    ]
    assert [item["prediction_ms"] for item in lattice[0]["predictions"]] == pytest.approx(
        [1200.0, 1200.0, 1200.0]
    )
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
    assert result.tool_resource["prediction"] is None
    latency = result.tool_resource["continuous_predictions"]["latency_ms"]
    assert latency["scope"] == "repo"
    assert latency["key_kind"] == "command_prefix_depth_3"
    assert latency["fallback_path"] == [
        "repo:exact_command",
        "repo:command_prefix_depth_4",
        "repo:command_prefix_depth_3",
    ]


def test_openclaw_trace_cold_start_persists_runtime_kb(tmp_path: Path) -> None:
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

    assert not (artifact_dir / "clause-resource-kb.json").is_file()
    assert (artifact_dir / "runtime-tool-resource-kb.json").is_file()

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

    assert result.resource_class == "latency_medium"
    assert result.tool_resource["prediction"] is None
    assert result.tool_resource["continuous_predictions"]["latency_ms"]["key_kind"] == "command_prefix_depth_3"


def test_shipped_runtime_snapshot_produces_public_predictions_for_any_repo(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "tool-resource"
    artifact_dir.mkdir()
    source = (
        Path(__file__).resolve().parents[3]
        / "traces"
        / "tool-resource"
        / "runtime-tool-resource-kb.json"
    )
    shutil.copyfile(source, artifact_dir / "runtime-tool-resource-kb.json")
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0, 10_000.0)),
        repo="12rambau/sepal_ui",
        artifact_dir=artifact_dir,
    )

    result = asyncio.run(
        predictor.predict(_tool_request("evt-public-runtime", "call-public-runtime", "git status"))
    )

    continuous = result.tool_resource["continuous_predictions"]
    assert continuous["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)
    assert continuous["latency_ms"]["scope"] == "public"
    assert continuous["latency_ms"]["key_kind"] == "global"
    assert continuous["latency_ms"]["evidence_count"] == 38
    assert continuous["peak_cpu_cores"]["conditional_p90"] == pytest.approx(1.5)
    assert continuous["peak_cpu_cores"]["scope"] == "public"
    assert continuous["peak_cpu_cores"]["evidence_count"] == 38


def test_shared_snapshots_reuse_same_repo_evidence_but_isolate_other_repos() -> None:
    snapshot_dir = (
        Path(__file__).resolve().parents[3] / "traces" / "tool-resource"
    )
    runtime = RuntimeToolResourceKB.from_json_obj(
        json.loads(
            (snapshot_dir / "runtime-tool-resource-kb.json").read_text(
                encoding="utf-8"
            )
        )
    )
    runtime_base_ts = (runtime._last_query_ts or 0.0) + 1.0
    runtime.observe_completed_call(
        CompletedCall(
            repo="12rambau/sepal_ui",
            tool_name="exec",
            command="git status",
            ts_start=runtime_base_ts,
            ts_end=runtime_base_ts + 0.2,
            peak_cpu_cores=0.25,
            peak_cpu_cores_eligible=True,
            peak_memory_mb=64.0,
            peak_memory_mb_eligible=True,
            ambient_before_mb=40.0,
        )
    )
    same_repo = runtime.query(
        ToolCallQuery(
            repo="12rambau/sepal_ui",
            tool_name="exec",
            command="git status",
            ts_start=runtime_base_ts + 2.0,
            ambient_before_mb=50.0,
        )
    )
    restored_runtime = RuntimeToolResourceKB.from_json_obj(runtime.to_json_obj())
    other_repo = restored_runtime.query(
        ToolCallQuery(
            repo="other/project",
            tool_name="exec",
            command="git status",
            ts_start=runtime_base_ts + 3.0,
            ambient_before_mb=60.0,
        )
    )

    assert same_repo["latency_ms"].scope == "repo"
    assert same_repo["latency_ms"].conditional_p90 == pytest.approx(200.0)
    assert same_repo["peak_memory_mb"].conditional_p90 == pytest.approx(74.0)
    assert other_repo["latency_ms"].scope == "public"
    assert other_repo["latency_ms"].conditional_p90 == pytest.approx(1200.0)
    assert other_repo["peak_memory_mb"].scope == "public"
    assert other_repo["peak_memory_mb"].conditional_p90 == pytest.approx(160.0)

    clause = ClauseResourceKB.from_json_obj(
        json.loads(
            (snapshot_dir / "clause-resource-kb.json").read_text(
                encoding="utf-8"
            )
        )
    )
    clause_base_ts = (clause._last_query_ts or 0.0) + 1.0
    clause.observe_completed_clause(
        ClauseObservation(
            repo="12rambau/sepal_ui",
            bin="git",
            argv=("git", "status"),
            ts_start=clause_base_ts,
            ts_end=clause_base_ts + 0.2,
            latency_ms=200.0,
        )
    )
    buckets = LatencyBuckets((100.0, 500.0, 2_000.0, 10_000.0))
    same_clause = clause.predict_command_latency_bucket(
        "12rambau/sepal_ui", "git status", clause_base_ts + 2.0, buckets
    )
    restored_clause = ClauseResourceKB.from_json_obj(clause.to_json_obj())
    other_clause = restored_clause.predict_command_latency_bucket(
        "other/project", "git status", clause_base_ts + 3.0, buckets
    )

    assert same_clause.prediction is not None
    assert same_clause.prediction.scope == "repo"
    assert other_clause.prediction is not None
    assert other_clause.prediction.scope == "public"


def test_shipped_clause_snapshot_produces_public_global_single_clause_bucket(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "tool-resource"
    artifact_dir.mkdir()
    source = (
        Path(__file__).resolve().parents[3]
        / "traces"
        / "tool-resource"
        / "clause-resource-kb.json"
    )
    shutil.copyfile(source, artifact_dir / "clause-resource-kb.json")
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0, 10_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )

    result = asyncio.run(
        predictor.predict(_tool_request("evt-public", "call-public", "python -V"))
    )

    prediction = result.tool_resource["prediction"]
    assert predictor.report.kb_available is True
    assert prediction["bucket_id"] == 2
    assert prediction["scope"] == "public"
    assert prediction["key_kind"] == "global"
    assert prediction["evidence_count"] == 16
    assert result.tool_resource["clause_predictions"] == [
        {
            "clause_index": 0,
            "bin": "python",
            "argv": ["python", "-V"],
            "prediction": prediction,
            "unavailable_reason": None,
        }
    ]


def test_shipped_clause_snapshot_predicts_exec_clause_in_real_compound_command(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "tool-resource"
    artifact_dir.mkdir()
    source = (
        Path(__file__).resolve().parents[3]
        / "traces"
        / "tool-resource"
        / "clause-resource-kb.json"
    )
    shutil.copyfile(source, artifact_dir / "clause-resource-kb.json")
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0, 10_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )

    result = asyncio.run(
        predictor.predict(
            _tool_request(
                "evt-compound-public",
                "call-compound-public",
                "cd /workspace && python3 -m pytest tests -q",
            )
        )
    )

    tool_resource = result.tool_resource
    assert tool_resource["clause_bins"] == ["cd", "python3"]
    assert tool_resource["prediction"] is None
    assert tool_resource["unavailable_reason"] == "compound_command_uncomposed"
    assert len(tool_resource["clause_predictions"]) == 1
    clause = tool_resource["clause_predictions"][0]
    assert clause["clause_index"] == 1
    assert clause["bin"] == "python3"
    assert clause["prediction"]["bucket_id"] == 2
    assert clause["prediction"]["scope"] == "public"
    assert clause["prediction"]["key_kind"] == "global"
    assert clause["prediction"]["evidence_count"] == 16
    assert clause["unavailable_reason"] is None


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

    assert result.resource_class == "latency_medium"
    continuous = result.tool_resource["continuous_predictions"]
    without_continuous = result.tool_resource | {"continuous_predictions": {}}
    assert without_continuous == {
        "repo": "repo-1",
        "command": "python -m pytest && git status",
        "parse_failed": False,
        "clause_bins": ["python", "git"],
        "clause_predictions": [
            {
                "clause_index": 0,
                "bin": "python",
                "argv": ["python", "-m", "pytest"],
                "prediction": None,
                "unavailable_reason": "no_clause_latency_evidence",
            },
            {
                "clause_index": 1,
                "bin": "git",
                "argv": ["git", "status"],
                "prediction": None,
                "unavailable_reason": "no_clause_latency_evidence",
            },
        ],
        "prediction": None,
        "unavailable_reason": "compound_clause_evidence_incomplete",
        "continuous_predictions": {},
        "lattice_time_predictions": _unavailable_lattice_time_predictions(
            [
                ("python", ["python", "-m", "pytest"]),
                ("git", ["git", "status"]),
            ]
        ),
        "prediction_algorithms": _prediction_algorithms(),
    }
    assert continuous["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)
    assert continuous["latency_ms"]["key_kind"] == "command_prefix_depth_3"
    assert continuous["peak_cpu_cores"]["conditional_p90"] == 1.5
    assert continuous["peak_cpu_cores"]["key_kind"] == "command_prefix_depth_3"
    assert continuous["peak_memory_mb"]["note"] == "memory prediction requires ambient_before_mb anchor"


def test_compound_prediction_requires_evidence_for_every_effective_clause() -> None:
    kb = ClauseResourceKB()
    buckets = LatencyBuckets((100.0, 500.0))
    clauses = (
        {"bin": "python", "argv": ("python", "-m", "pytest")},
        {"bin": "apt-get", "argv": ("apt-get", "update")},
    )
    kb.observe_completed_clause(
        ClauseObservation(
            repo="repo-1",
            bin="python",
            argv=("python", "-m", "pytest"),
            ts_start=1.0,
            ts_end=1.08,
            latency_ms=80.0,
        )
    )

    incomplete = kb.predict_command_latency_bucket_from_clauses(
        "repo-1",
        clauses,
        2.0,
        buckets,
        command="python -m pytest && apt-get update",
    )

    assert incomplete.prediction is None
    assert incomplete.unavailable_reason == "compound_clause_evidence_incomplete"
    assert incomplete.clause_bins == ("python", "apt-get")
    assert [item.clause_index for item in incomplete.clause_predictions] == [0, 1]
    assert incomplete.clause_predictions[0].prediction is not None
    assert incomplete.clause_predictions[0].unavailable_reason is None
    assert incomplete.clause_predictions[1].prediction is None
    assert (
        incomplete.clause_predictions[1].unavailable_reason
        == "no_clause_latency_evidence"
    )

    kb.observe_completed_clause(
        ClauseObservation(
            repo="repo-1",
            bin="apt-get",
            argv=("apt-get", "update"),
            ts_start=2.1,
            ts_end=3.0,
            latency_ms=900.0,
        )
    )
    complete = kb.predict_command_latency_bucket_from_clauses(
        "repo-1",
        clauses,
        4.0,
        buckets,
        command="python -m pytest && apt-get update",
    )

    assert complete.prediction is None
    assert complete.unavailable_reason == "compound_command_uncomposed"
    assert all(item.prediction is not None for item in complete.clause_predictions)
    assert all(item.unavailable_reason is None for item in complete.clause_predictions)


def test_compound_prediction_ignores_noexec_builtins_but_preserves_raw_bins() -> None:
    kb = ClauseResourceKB()
    buckets = LatencyBuckets((100.0, 500.0))
    kb.observe_completed_clause(
        ClauseObservation(
            repo="repo-1",
            bin="python",
            argv=("python", "-m", "pytest"),
            ts_start=1.0,
            ts_end=1.2,
            latency_ms=200.0,
        )
    )

    compound = kb.predict_command_latency_bucket_from_clauses(
        "repo-1",
        (
            {"bin": "cd", "argv": ("cd", "/workspace")},
            {"bin": "python", "argv": ("python", "-m", "pytest")},
        ),
        2.0,
        buckets,
        command="cd /workspace && python -m pytest",
    )

    assert compound.clause_bins == ("cd", "python")
    assert compound.prediction is None
    assert compound.unavailable_reason == "compound_command_uncomposed"
    assert len(compound.clause_predictions) == 1
    assert compound.clause_predictions[0].clause_index == 1
    assert compound.clause_predictions[0].bin == "python"
    assert compound.clause_predictions[0].prediction is not None

    builtin_only = kb.predict_command_latency_bucket_from_clauses(
        "repo-1",
        ({"bin": "cd", "argv": ("cd", "/workspace")},),
        3.0,
        buckets,
        command="cd /workspace",
    )

    assert builtin_only.clause_bins == ("cd",)
    assert builtin_only.clause_predictions == ()
    assert builtin_only.prediction is None
    assert builtin_only.unavailable_reason == "no_executable_clauses"

    builtin_compound = kb.predict_command_latency_bucket_from_clauses(
        "repo-1",
        (
            {"bin": "cd", "argv": ("cd", "/workspace")},
            {"bin": "export", "argv": ("export", "MODE=test")},
        ),
        4.0,
        buckets,
        command="cd /workspace && export MODE=test",
    )

    assert builtin_compound.clause_bins == ("cd", "export")
    assert builtin_compound.clause_predictions == ()
    assert builtin_compound.prediction is None
    assert builtin_compound.unavailable_reason == "compound_command_uncomposed"


@pytest.mark.parametrize("tool_name", ["read", "edit"])
def test_commandless_repo_tool_name_learning_is_causal_and_survives_snapshot(
    tool_name: str,
) -> None:
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(
        CompletedCall(
            repo="repo-1",
            tool_name=tool_name,
            command=None,
            ts_start=1.0,
            ts_end=1.2,
            peak_cpu_cores=0.25,
            peak_cpu_cores_eligible=True,
            peak_memory_mb=64.0,
            peak_memory_mb_eligible=True,
            ambient_before_mb=40.0,
        )
    )

    second_call = kb.query(
        ToolCallQuery(
            repo="repo-1",
            tool_name=tool_name,
            command=None,
            ts_start=2.0,
            ambient_before_mb=50.0,
        )
    )

    assert second_call["latency_ms"].conditional_p90 == pytest.approx(200.0)
    assert second_call["peak_cpu_cores"].conditional_p90 == pytest.approx(0.25)
    assert second_call["peak_memory_mb"].conditional_p90 == pytest.approx(74.0)
    for prediction in second_call.values():
        assert prediction.scope == "repo"
        assert prediction.key_kind == "tool_name"
        assert prediction.evidence_count == 1
        assert prediction.fallback_path == ("repo:tool_name",)

    kb.observe_completed_call(
        CompletedCall(
            repo="repo-1",
            tool_name=tool_name,
            command=None,
            ts_start=2.1,
            ts_end=2.5,
            peak_cpu_cores=0.5,
            peak_cpu_cores_eligible=True,
            peak_memory_mb=80.0,
            peak_memory_mb_eligible=True,
            ambient_before_mb=50.0,
        )
    )
    restored = RuntimeToolResourceKB.from_json_obj(kb.to_json_obj())

    third_call = restored.query(
        ToolCallQuery(
            repo="repo-1",
            tool_name=tool_name,
            command=None,
            ts_start=3.0,
            ambient_before_mb=60.0,
        )
    )

    assert third_call["latency_ms"].conditional_p90 == pytest.approx(400.0)
    assert third_call["peak_cpu_cores"].conditional_p90 == pytest.approx(0.5)
    assert third_call["peak_memory_mb"].conditional_p90 == pytest.approx(90.0)
    assert all(prediction.evidence_count == 2 for prediction in third_call.values())


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
    assert result.duration_p50_ms == 1200
    assert result.duration_p90_ms == 1200
    assert result.tool_resource["prediction"] is None
    assert result.tool_resource["unavailable_reason"] == "no_clause_latency_evidence"
    assert result.tool_resource["continuous_predictions"]["peak_cpu_cores"][
        "conditional_p90"
    ] == 0.8
    assert result.tool_resource["continuous_predictions"]["peak_memory_mb"][
        "note"
    ] == "memory prediction requires ambient_before_mb anchor"


def test_completion_correlation_isolated_by_runtime_owner() -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    first = _tool_request("evt-start-a", "shared-call", "python task_a.py").model_copy(
        update={"gateway_id": "gateway-a", "runtime_id": "runtime-a"}
    )
    second = _tool_request("evt-start-b", "shared-call", "python task_b.py").model_copy(
        update={"gateway_id": "gateway-b", "runtime_id": "runtime-b"}
    )
    predictor.record_tool_started(first)
    predictor.record_tool_started(second)

    # Omitting the optional agent identity forces the legacy-compatible lookup
    # while the supplied runtime owner still makes the match unambiguous.
    completion = _tool_completion("evt-end-b", "shared-call").model_copy(
        update={
            "gateway_id": "gateway-b",
            "runtime_id": "runtime-b",
            "agent_id": None,
        }
    )
    assert predictor.observe_completion(
        completion,
        _runtime_sample("evt-end-b", "shared-call"),
    ) == 1

    remaining = list(predictor._starts.values())
    assert remaining == [first]


def test_legacy_completion_does_not_guess_between_ambiguous_runtime_owners() -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    predictor.record_tool_started(
        _tool_request("evt-start-a", "shared-call", "python task_a.py").model_copy(
            update={"gateway_id": "gateway-a", "runtime_id": "runtime-a"}
        )
    )
    predictor.record_tool_started(
        _tool_request("evt-start-b", "shared-call", "python task_b.py").model_copy(
            update={"gateway_id": "gateway-b", "runtime_id": "runtime-b"}
        )
    )

    completion = _tool_completion("evt-end-legacy", "shared-call").model_copy(
        update={"agent_id": None}
    )
    assert predictor.observe_completion(
        completion,
        _runtime_sample("evt-end-legacy", "shared-call"),
    ) == 1

    # A legacy peer without gateway/runtime identity is compatible with both
    # starts, so neither owner is consumed or misattributed.
    assert len(predictor._starts) == 2


@pytest.mark.parametrize(
    "attribution_source",
    ["shared-sandbox-container", "shared-runtime-process"],
)
def test_live_shared_scope_keeps_only_runtime_latency(
    attribution_source: str,
) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    scope = ResourceScope(
        kind="cgroup-v2",
        cgroup_path="/sys/fs/cgroup/shared-sandbox",
        source="openclaw-sandbox",
        attribution_source=attribution_source,
    )
    request = _tool_request(
        "evt-shared",
        "call-shared",
        "python -m pytest tests -q",
    ).model_copy(update={"resource_scope": scope})
    completion = ToolCompletedEvent(
        schema_version="scheduler.v1",
        event_id="evt-shared",
        occurred_at="2026-07-24T17:29:45Z",
        plugin_version="0.1.0",
        run_id="run-1",
        session_id="session-1",
        session_key=None,
        agent_id="main",
        tool_call_id="call-shared",
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
        resource_scope=scope,
    )
    sample = _runtime_sample("evt-shared", "call-shared")

    completed = tool_resource_predictor.completed_call_from_completion(
        completion,
        sample,
        repo="repo-1",
        start=request,
    )
    assert completed is not None
    assert completed.peak_cpu_cores == pytest.approx(0.8)
    assert completed.peak_cpu_cores_eligible is False
    assert completed.peak_memory_mb == pytest.approx(100.0)
    assert completed.peak_memory_mb_eligible is False

    clause = tool_resource_predictor.observation_from_completion(
        completion,
        sample,
        repo="repo-1",
        start=request,
    )
    assert clause is not None
    assert clause.latency_ms == pytest.approx(1200.0)
    assert clause.peak_cpu_cores is None
    assert clause.sampled_peak_rss_mb is None
    assert clause.cpu_ns_cumulative is None

    predictor.record_tool_started(request)
    assert predictor.observe_completion(completion, sample) == 1
    prediction = asyncio.run(
        predictor.predict(
            _tool_request(
                "evt-shared-next",
                "call-shared-next",
                "python -m pytest tests -q",
            ),
            ambient_before_mb=50.0,
        )
    )
    continuous = prediction.tool_resource["continuous_predictions"]
    assert continuous["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)
    assert continuous["peak_cpu_cores"]["conditional_p90"] is None
    assert continuous["peak_memory_mb"]["conditional_p90"] is None


def test_tool_resource_predictor_explains_unknown_without_cold_start() -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )

    result = asyncio.run(
        predictor.predict(_tool_request("evt-1", "call-1", "python -m pytest tests -q"))
    )

    assert result.resource_class == "unknown"
    assert result.tool_resource["repo"] == "repo-1"
    assert result.tool_resource["command"] == "python -m pytest tests -q"
    assert result.tool_resource["clause_bins"] == ["python"]
    assert result.tool_resource["prediction"] is None
    assert result.tool_resource["unavailable_reason"] == "no_clause_latency_evidence"
    continuous = result.tool_resource["continuous_predictions"]
    assert continuous["latency_ms"]["note"] == "no continuous evidence for target"
    assert continuous["peak_cpu_cores"]["note"] == "no continuous evidence for target"
    assert continuous["peak_memory_mb"]["note"] == "memory prediction requires ambient_before_mb anchor"
    assert [item["name"] for item in result.tool_resource["prediction_algorithms"]["enabled"]] == [
        "clause_latency_bucket",
        "lattice_shrinkage",
        "lattice_loso",
        "lattice_max_cardinality",
        "runtime_tool_resource_conditional_p90",
    ]


def test_lattice_time_predictions_are_limited_to_ebpf_exec_clauses() -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )
    predictor.lattice_kb.merge_historical(
        [
            ClauseObservation(
                repo="repo-1",
                bin="python",
                argv=("python", "task.py"),
                ts_start=1.0,
                ts_end=2.0,
                latency_ms=250.0,
            )
        ]
    )
    request = _tool_request("evt-read", "call-read", "ignored").model_copy(
        update={
            "tool_name": "read",
            "tool_kind": "file",
            "raw_params": {"path": "/workspace/task.py"},
        }
    )

    result = asyncio.run(predictor.predict(request))

    assert result.tool_resource["lattice_time_predictions"] == []


def test_finish_execution_feeds_and_persists_the_shared_lattice_kb(
    tmp_path: Path,
    monkeypatch,
) -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
        artifact_dir=tmp_path,
    )
    now = time.time()
    observation = ClauseObservation(
        repo="repo-1",
        bin="python",
        argv=("python", "task.py"),
        ts_start=now - 2.0,
        ts_end=now - 1.0,
        latency_ms=250.0,
    )
    run = SimpleNamespace(
        tool_call_id="call-online",
        _observer=SimpleNamespace(
            context=SimpleNamespace(artifact_path=tmp_path / "call-online.json")
        ),
    )
    predictor._runs_by_execution_id["exec-online"] = run
    prepare_threads: list[str] = []
    original_prepare = predictor.lattice_kb.prepare

    def track_prepare() -> None:
        prepare_threads.append(threading.current_thread().name)
        original_prepare()

    monkeypatch.setattr(predictor.lattice_kb, "prepare", track_prepare)
    monkeypatch.setattr(
        predictor._sdk,
        "finish_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            kb_observations=(observation,),
            kb_observations_added=1,
            kb_update_error=None,
            call_telemetry={},
            telemetry_artifact=None,
        ),
    )

    summary = predictor.finish_execution(
        execution_id="exec-online",
        exit_code=0,
        signal=None,
        succeeded=True,
    )
    predictor.flush_kb_updates(timeout_seconds=2.0)

    assert summary.kb_update_error is None
    assert prepare_threads == ["clawtune-kb-writer"]
    snapshot = json.loads(
        (tmp_path / "clause-lattice-time-kb.json").read_text(encoding="utf-8")
    )
    assert len(snapshot["pending"]) == 1
    monkeypatch.setattr(
        "tool_time.lattice_kb._build_node_state",
        lambda _observations: pytest.fail("prediction rebuilt the prepared lattice"),
    )

    prediction = asyncio.run(
        predictor.predict(_tool_request("evt-online", "call-next", "python task.py"))
    )
    outcomes = prediction.tool_resource["lattice_time_predictions"][0]["predictions"]
    assert [item["prediction_ms"] for item in outcomes] == pytest.approx(
        [250.0, 250.0, 250.0]
    )


def test_tool_resource_predictor_explains_empty_continuous_memory_with_anchor() -> None:
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
    )

    result = asyncio.run(
        predictor.predict(
            _tool_request("evt-1", "call-1", "python -m pytest tests -q"),
            ambient_before_mb=10.0,
        )
    )

    memory = result.tool_resource["continuous_predictions"]["peak_memory_mb"]
    assert memory["conditional_p90"] is None
    assert memory["note"] == "no continuous evidence for target"


def test_exec_prediction_uses_fallback_parser_when_mvdan_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    command = "python -m pytest tests -q"
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, command=command)
    predictor = ToolResourcePredictor.from_openclaw_traces(
        [trace],
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0, 10_000.0)),
        repo="repo-1",
    )

    def fail_parse(_command: str) -> dict:
        raise RuntimeError("mvdan unavailable")

    monkeypatch.setattr(tool_resource_predictor, "parse_command_clauses", fail_parse)
    monkeypatch.setattr(tool_resource_runtime_kb, "parse_command_clauses", fail_parse)

    result = asyncio.run(
        predictor.predict(_tool_request("evt-1", "call-1", command))
    )

    assert result.resource_class == "latency_medium"
    assert result.tool_resource["unavailable_reason"] == "no_clause_latency_evidence"
    assert result.tool_resource["prediction"] is None
    assert result.tool_resource["continuous_predictions"]["latency_ms"]["scope"] == "repo"
    assert result.tool_resource["continuous_predictions"]["latency_ms"]["key_kind"] == "exact_command"


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
    predictor.flush_kb_updates(timeout_seconds=2.0)
    assert not (artifact_dir / "clause-resource-kb.json").is_file()
    assert (artifact_dir / "runtime-tool-resource-kb.json").is_file()

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
    assert result.tool_resource["prediction"] is None
    assert result.tool_resource["unavailable_reason"] == "no_clause_latency_evidence"
    assert result.tool_resource["continuous_predictions"]["latency_ms"]["key_kind"] == "command_prefix_depth_3"


def test_tool_resource_predictor_concurrent_completions_persist_without_lost_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifact_dir = tmp_path / "tool-resource"
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )
    predictor._kb_writes = tool_resource_predictor._KnowledgeBaseWriteCoordinator(
        predictor._flush_kb_batch,
        batch_window_s=0.2,
    )
    writes: list[tuple[Path, str]] = []
    original_write = tool_resource_predictor._write_json_atomic

    def track_write(path: Path, obj) -> None:
        writes.append((path, threading.current_thread().name))
        original_write(path, obj)

    monkeypatch.setattr(tool_resource_predictor, "_write_json_atomic", track_write)
    predictor.record_tool_started(_tool_request("evt-1", "call-1", "python task_a.py"))
    predictor.record_tool_started(_tool_request("evt-2", "call-2", "python task_b.py"))

    def complete(event_id: str, tool_call_id: str) -> int:
        return predictor.observe_completion(
            ToolCompletedEvent(
                schema_version="scheduler.v1",
                event_id=event_id,
                occurred_at="2026-07-24T17:29:45Z",
                plugin_version="0.1.0",
                run_id="run-1",
                session_id="session-1",
                session_key=None,
                agent_id="main",
                tool_call_id=tool_call_id,
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
            _runtime_sample(event_id, tool_call_id),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: complete(*args),
                [("evt-1", "call-1"), ("evt-2", "call-2")],
            )
        )

    assert results == [1, 1]
    predictor.flush_kb_updates(timeout_seconds=2.0)
    runtime_writes = [
        (path, thread_name)
        for path, thread_name in writes
        if path.name == "runtime-tool-resource-kb.json"
    ]
    assert runtime_writes == [
        (artifact_dir / "runtime-tool-resource-kb.json", "clawtune-kb-writer")
    ]
    reloaded = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(),
        stage2_trace_paths=(),
        buckets=LatencyBuckets((100.0, 500.0, 2_000.0)),
        repo="repo-1",
        artifact_dir=artifact_dir,
    )
    first = asyncio.run(
        reloaded.predict(_tool_request("evt-next-1", "call-next-1", "python task_a.py"))
    )
    second = asyncio.run(
        reloaded.predict(_tool_request("evt-next-2", "call-next-2", "python task_b.py"))
    )

    assert first.tool_resource["continuous_predictions"]["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)
    assert second.tool_resource["continuous_predictions"]["latency_ms"]["conditional_p90"] == pytest.approx(1200.0)


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
            tool_resource_artifact_dir=tmp_path / "tool-resource",
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
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "contracts"
            / "tool-decision.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(response.json())
    assert response.json()["prediction"]["resource_class"] == "latency_medium"


def test_sidecar_defaults_exec_memory_anchor_for_new_execution_cgroup(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, memory_rss_bytes_before=0)
    state = build_state(
        SchedulerConfig(
            tool_resource_trace_paths=(trace,),
            tool_resource_latency_buckets_ms=(100.0, 500.0, 2_000.0),
            tool_resource_artifact_dir=tmp_path / "tool-resource",
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
    memory = response.json()["prediction"]["tool_resource"]["continuous_predictions"][
        "peak_memory_mb"
    ]
    assert memory["conditional_p90"] == pytest.approx(100.0)
    assert memory["scope"] == "repo"
    assert memory["key_kind"] == "exact_command"
    assert memory["note"] == "residual quantile plus query ambient_before_mb"


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
    assert tool_start["prediction"]["duration_p50_ms"] == 1200
    assert tool_start["prediction"]["duration_p90_ms"] == 1200
    assert tool_start["prediction"]["tool_resource"]["prediction"] is None
    assert (
        tool_start["prediction"]["tool_resource"]["continuous_predictions"]["latency_ms"]["key_kind"]
        == "exact_command"
    )
    algorithms = tool_start["prediction"]["tool_resource"]["prediction_algorithms"]
    assert [item["name"] for item in algorithms["enabled"]] == [
        "clause_latency_bucket",
        "lattice_shrinkage",
        "lattice_loso",
        "lattice_max_cardinality",
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


def _tool_completion(event_id: str, tool_call_id: str) -> ToolCompletedEvent:
    return ToolCompletedEvent(
        schema_version="scheduler.v1",
        event_id=event_id,
        occurred_at="2026-07-24T17:29:45Z",
        plugin_version="0.1.0",
        run_id="run-1",
        session_id="session-1",
        session_key=None,
        agent_id="main",
        tool_call_id=tool_call_id,
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
                "name": "lattice_shrinkage",
                "family": "context_lattice",
                "source": "LatticeTimeKB",
                "targets": ["latency_ms"],
                "outputs": ["clause_point_prediction_ms"],
            },
            {
                "name": "lattice_loso",
                "family": "context_lattice",
                "source": "LatticeTimeKB",
                "targets": ["latency_ms"],
                "outputs": ["clause_point_prediction_ms"],
            },
            {
                "name": "lattice_max_cardinality",
                "family": "context_lattice",
                "source": "LatticeTimeKB",
                "targets": ["latency_ms"],
                "outputs": ["clause_point_prediction_ms"],
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


def _unavailable_lattice_time_predictions(
    clauses: list[tuple[str, list[str]]],
) -> list[dict]:
    return [
        {
            "clause_index": clause_index,
            "bin": bin_,
            "argv": argv,
            "predictions": [
                {
                    "algorithm": algorithm,
                    "prediction_ms": None,
                    "selected_features": [],
                    "evidence_count": 0,
                    "selected_risk": None,
                    "exact_match": None,
                    "fallback": None,
                    "unavailable_reason": "no_lattice_time_evidence",
                }
                for algorithm in ("shrinkage", "loso", "max_cardinality")
            ],
        }
        for clause_index, (bin_, argv) in enumerate(clauses)
    ]
