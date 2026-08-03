from __future__ import annotations

import json
import time
from pathlib import Path

from swe_rebench.docker import ContainerResult
from swe_rebench.config import RunnerConfig
from swe_rebench.task_source import TaskDef
import swe_rebench.host_sandbox as host_sandbox
import swe_rebench.runner as runner
from swe_rebench.runner import (
    _agent_diagnostics,
    _inspect_tool_resource_artifacts,
    _inspect_trace,
    _required_telemetry_error,
    _resource_summary,
    _stage2_call_lifecycle_complete,
)


def _stage2_call_provenance(root_pid: int = 1234) -> dict:
    return {
        "command_tree": {
            "status": "ok",
            "reason": None,
            "entry_pid": root_pid,
            "root_pids": [root_pid],
            "identity_anchor": {
                "kind": "launcher_started",
                "host_pid": root_pid,
            },
        },
        "event_isolation": {
            "mode": "trusted_execution_root",
            "trusted_root_pid": root_pid,
        },
    }


def _stage2_pid_namespace_remap_provenance(
    root_pid: int = 612345,
    claimed_root_pid: int = 1234,
) -> dict:
    provenance = _stage2_call_provenance(root_pid)
    provenance["event_isolation"] = {
        "mode": "trusted_execution_root_pid_namespace_remap",
        "trusted_root_pid": root_pid,
        "claimed_trusted_root_pid": claimed_root_pid,
        "remap_evidence": "exact_registered_root_shell",
        "selected_pid_count": 2,
        "raw_window_event_count": 12,
        "selected_event_count": 8,
    }
    return provenance


def _stage2_artifact(
    call_id: str,
    *,
    quality: str = "ok",
    eligible_for_kb: bool = True,
    invalid_reasons: list[dict] | None = None,
    clauses: list[dict] | None = None,
) -> dict:
    return {
        "schema": "clause_telemetry_v2",
        "version": 2,
        "mode": "clause",
        "status_model": "call_granular_v1",
        "telemetry_quality": quality,
        "collection_validity": "valid" if quality == "ok" else "invalid",
        "formal_completeness": "complete" if quality == "ok" else "partial",
        "integrity": {"status": "ok" if quality == "ok" else "failed"},
        "cleanup": "ok",
        "replay_execution": "completed",
        "container_id": "container-1",
        "cgroup_id": 42,
        "ring_loss_total": 0,
        "telemetry_loss_total": {"total": 0},
        "collector": {
            "health": "healthy",
            "state_before_close": "active",
            "state": "closed",
            "disabled_reason": None,
            "kprobe_total_hits": 10,
        },
        "calls": [
            {
                "tool_call_id": call_id,
                "tool_trace_ref": call_id,
                "command": "printf hi",
                "telemetry_quality": quality,
                "eligible_for_kb": eligible_for_kb,
                "invalid_reasons": invalid_reasons or [],
                "provenance": _stage2_call_provenance(),
                "clauses": clauses or [],
                "no_runtime_exec": [],
            }
        ],
    }


def test_stage2_lifecycle_accepts_strict_pid_namespace_remap() -> None:
    call = {
        "tool_call_id": "call-remapped",
        "tool_trace_ref": "call-remapped",
        "provenance": _stage2_pid_namespace_remap_provenance(),
    }

    assert _stage2_call_lifecycle_complete(call) is True

    invalid_mutations = [
        ("claimed_trusted_root_pid", None),
        ("claimed_trusted_root_pid", 612345),
        ("claimed_trusted_root_pid", True),
        ("remap_evidence", "argv_heuristic"),
        ("selected_pid_count", 0),
        ("raw_window_event_count", 0),
        ("selected_event_count", 0),
        ("selected_event_count", 13),
    ]
    for field, value in invalid_mutations:
        invalid_call = json.loads(json.dumps(call))
        invalid_call["provenance"]["event_isolation"][field] = value
        assert _stage2_call_lifecycle_complete(invalid_call) is False


def _stage2_clause(bin_: str = "printf", exit_code: int = 0) -> dict:
    return {
        "bin": bin_,
        "status": {
            "state": "exited",
            "exit_code": exit_code,
            "signal": None,
            "succeeded": exit_code == 0,
            "reason": None,
            "source": "root_exec_chain_terminal",
        },
    }


def _stage2_span_end(call_id: str, *, status: str = "invalid") -> dict:
    return {
        "record_type": "span_end",
        "span_id": call_id,
        "kind": "tool",
        "name": "exec",
        "status": {"code": "ok"},
        "output": {"exit_code": 0},
        "execution": {
            "mode": "launcher",
            "execution_id": call_id,
            "tool_resource": {
                "status": status,
                "started": True,
                "execution_id": call_id,
                "tool_call_id": call_id,
                "artifact_path": f"/tmp/tool-resource/{call_id}.json",
                "artifact_summary": {
                    "schema": "clause_telemetry_v2",
                    "call_count": 1,
                    "collector": {"health": "healthy"},
                },
                "call_telemetry": {
                    "tool_call_id": call_id,
                    "telemetry_quality": status,
                },
                "kb_observations_added": 0,
            },
        },
        "resources": {
            "scope": "cgroup",
            "monitor_source": "cgroup-v2",
            "attribution_status": "attributed",
            "sampling_point_count": 1,
            "cpu_time_s": 0.1,
            "rss_peak_bytes": 1024,
            "resource_timeline": [{"available": True}],
        },
    }


def _stage2_prediction_start() -> dict:
    return {
        "record_type": "span_start",
        "kind": "tool",
        "name": "exec",
        "prediction": {
            "tool_resource": {
                "prediction": {
                    "bucket_id": 0,
                    "probability_by_bucket": [1.0, 0.0],
                    "scope": "repo",
                    "key_kind": "exact_clause",
                    "evidence_count": 1,
                    "fallback_path": ["repo:exact_clause"],
                },
                "continuous_predictions": {
                    target: {
                        "target": target,
                        "conditional_p90": value,
                        "scope": "repo",
                        "key_kind": "exact_command",
                        "evidence_count": 1,
                        "fallback_path": ["repo:exact_command"],
                        "note": None,
                    }
                    for target, value in (
                        ("latency_ms", 10.0),
                        ("peak_cpu_cores", 1.0),
                        ("peak_memory_mb", 32.0),
                    )
                },
            }
        },
    }


def test_empty_llm_response_is_reported_as_agent_failure_before_telemetry(tmp_path):
    trace = tmp_path / "trace.jsonl"
    records = [
        {"record_type": "trace_metadata", "task": "task-1"},
        {
            "record_type": "span_end",
            "kind": "llm",
            "name": "model",
            "status": {"code": "ok"},
            "output": {"content": {"message": {"role": "assistant", "content": ""}}},
        },
    ]
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    inspected = _inspect_trace(trace, "task-1")
    diagnostics = _agent_diagnostics(
        [inspected],
        {
            "agent-stderr.txt": {
                "preview": "empty response retries exhausted: provider=test"
            },
            "model.patch": {"bytes": 0, "has_diff": False},
        },
        {"agent_exit_code": 0, "has_patch": False},
    )

    assert inspected["llm_span_ends"] == 1
    assert inspected["empty_llm_span_ends"] == 1
    assert diagnostics["failure_kind"] == "empty_llm_response"
    assert "never entered the tool-execution phase" in diagnostics["failure"]


def test_trace_inspection_counts_failed_and_unattributed_launcher_spans(tmp_path):
    trace = tmp_path / "trace.jsonl"
    records = [
        {"record_type": "trace_metadata", "task": "task-1"},
        {
            "record_type": "span_end",
            "kind": "tool",
            "name": "exec",
            "status": {"code": "error"},
            "output": {"exit_code": 1},
            "execution": {"mode": "launcher", "tool_resource": None},
            "resources": {
                "scope": "cgroup",
                "attribution_status": "unattributed",
            },
        },
    ]
    trace.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    inspected = _inspect_trace(trace, "task-1")
    summary = _resource_summary([inspected])

    assert inspected["failed_tool_span_ends"] == 1
    assert inspected["unattributed_launcher_tool_span_ends"] == 1
    assert inspected["launcher_tool_resource_span_ends"] == 0
    assert "trace contains failed tool spans" in inspected["warnings"]
    assert "launcher tool spans have no Stage-2 tool-resource telemetry" in inspected["warnings"]
    assert summary["cgroup_coverage_ratio"] == 1.0
    assert summary["launcher_attribution_ratio"] == 0.0
    assert summary["launcher_tool_resource_ratio"] == 0.0


def test_launcher_permission_failure_and_invalid_coverage_are_root_diagnostics(tmp_path):
    trace = tmp_path / "trace.jsonl"
    record = {
        "record_type": "span_end",
        "kind": "tool",
        "name": "exec",
        "status": {"code": "error"},
        "output": {
            "exit_code": 126,
            "result": {"details": {"failureKind": "shell-not-executable"}},
        },
        "execution": {"mode": "launcher", "execution_id": "call-1"},
        "resources": {
            "scope": "cgroup",
            "attribution_status": "partially_attributed",
            "coverage_ratio": 4.5,
        },
    }
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")

    inspected = _inspect_trace(trace, "")
    diagnostics = _agent_diagnostics(
        [inspected],
        {"model.patch": {"bytes": 0, "has_diff": False}},
        {"agent_exit_code": 0, "has_patch": False},
    )

    assert inspected["launcher_not_executable_span_ends"] == 1
    assert inspected["invalid_coverage_ratio_span_ends"] == 1
    assert diagnostics["failure_kind"] == "launcher_not_executable"


def test_launcher_bare_run_failure_is_reported_before_no_patch(tmp_path):
    trace = tmp_path / "trace.jsonl"
    record = {
        "record_type": "span_end",
        "kind": "tool",
        "name": "exec",
        "status": {"code": "error"},
        "output": {
            "exit_code": 127,
            "stderr": "/bin/bash: run: No such file or directory",
            "result": {"details": {"failureKind": "shell-command-not-found"}},
        },
        "execution": {"mode": "launcher", "execution_id": "call-1"},
        "resources": {
            "scope": "cgroup",
            "attribution_status": "attributed",
        },
    }
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")

    inspected = _inspect_trace(trace, "")
    diagnostics = _agent_diagnostics(
        [inspected],
        {"model.patch": {"bytes": 0, "has_diff": False}},
        {"agent_exit_code": 0, "has_patch": False},
    )
    summary = _resource_summary([inspected])

    assert inspected["launcher_command_not_found_span_ends"] == 1
    assert summary["launcher_command_not_found_span_ends"] == 1
    assert "launcher command was not invoked correctly inside the sandbox" in inspected["warnings"]
    assert diagnostics["failure_kind"] == "launcher_invocation_command_not_found"
    assert diagnostics["launcher_command_not_found_span_ends"] == 1


def test_trace_inspection_reports_stage2_failures_and_prediction_fallbacks(tmp_path):
    trace = tmp_path / "trace.jsonl"
    records = [
        {"record_type": "trace_metadata", "task": "task-1"},
        {
            "record_type": "span_start",
            "kind": "tool",
            "name": "exec",
                "prediction": {
                    "tool_resource": {
                        "prediction": None,
                        "continuous_predictions": {
                            "latency_ms": {
                                "target": "latency_ms",
                                "conditional_p90": 100.0,
                                "scope": "repo",
                                "key_kind": "exact_command",
                                "evidence_count": 1,
                                "fallback_path": ["repo:exact_command"],
                                "note": None,
                            },
                            "peak_cpu_cores": {
                                "target": "peak_cpu_cores",
                                "conditional_p90": None,
                                "scope": None,
                                "key_kind": None,
                                "evidence_count": 0,
                                "fallback_path": [],
                                "note": "no evidence",
                            },
                        },
                    }
                },
        },
        {
            "record_type": "span_end",
            "kind": "tool",
            "name": "exec",
            "status": {"code": "ok"},
            "output": {"exit_code": 0},
            "execution": {
                "mode": "launcher",
                "tool_resource": {
                    "status": "unavailable",
                    "unavailable_reason": "call_telemetry_unavailable",
                    "artifact_summary": {
                        "collector": {
                            "disabled_reason": "collector attach failed: BPF module compile failed"
                        }
                    },
                },
            },
            "resources": {
                "scope": "cgroup",
                "attribution_status": "attributed",
            },
        },
    ]
    trace.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    inspected = _inspect_trace(trace, "task-1")
    summary = _resource_summary([inspected])

    assert inspected["launcher_tool_resource_unavailable_span_ends"] == 1
    assert inspected["launcher_tool_resource_unavailable_reasons"] == {
        "call_telemetry_unavailable": 1
    }
    assert inspected["launcher_tool_resource_disabled_reasons"] == {
        "collector attach failed: BPF module compile failed": 1
    }
    assert inspected["continuous_prediction_available_span_starts"] == 1
    assert inspected["tool_resource_prediction_available_span_starts"] == 1
    assert inspected["clause_bucket_prediction_available_span_starts"] == 0
    assert inspected[
        "continuous_latency_ms_prediction_available_span_starts"
    ] == 1
    assert inspected[
        "continuous_peak_cpu_cores_prediction_available_span_starts"
    ] == 0
    assert inspected[
        "continuous_peak_memory_mb_prediction_available_span_starts"
    ] == 0
    assert "Stage-2 tool-resource telemetry is unavailable for some launcher tool spans" in inspected["warnings"]
    assert summary["launcher_tool_resource_unavailable_span_ends"] == 1
    assert summary["tool_resource_prediction_available_ratio"] == 1.0


def test_trace_inspection_accepts_honest_compound_clause_bucket(tmp_path):
    trace = tmp_path / "trace.jsonl"
    bucket = {
        "bucket_id": 1,
        "probability_by_bucket": [0.0, 1.0, 0.0],
        "scope": "public",
        "key_kind": "global",
        "evidence_count": 8,
        "fallback_path": ["public:bin", "public:global"],
    }
    record = {
        "record_type": "span_start",
        "kind": "tool",
        "name": "exec",
        "prediction": {
            "tool_resource": {
                "prediction": None,
                "unavailable_reason": "compound_command_uncomposed",
                "clause_predictions": [
                    {
                        "clause_index": 1,
                        "bin": "python3",
                        "argv": ["python3", "-m", "pytest"],
                        "prediction": bucket,
                        "unavailable_reason": None,
                    }
                ],
                "continuous_predictions": {},
            }
        },
    }
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")

    inspected = _inspect_trace(trace, "")

    assert inspected["tool_resource_prediction_available_span_starts"] == 1
    assert inspected["clause_bucket_prediction_available_span_starts"] == 1


def test_required_telemetry_audits_all_tool_samples_and_async_artifacts(tmp_path):
    trace_dir = tmp_path / "trace"
    artifact_dir = trace_dir / "tool-resource"
    artifact_dir.mkdir(parents=True)
    # Fallback execution IDs are exec-<uuid>, not necessarily call_<id>.
    # The audit must discover artifacts by schema rather than filename prefix.
    (artifact_dir / "exec-async.json").write_text(
        json.dumps(_stage2_artifact("call_async", clauses=[_stage2_clause()])),
        encoding="utf-8",
    )
    (artifact_dir / "clause-resource-kb.json").write_text(
        json.dumps({"version": 1, "observations": []}),
        encoding="utf-8",
    )
    (artifact_dir / "runtime-tool-resource-kb.json").write_text(
        json.dumps({"schema": "runtime_tool_resource_kb_v1"}),
        encoding="utf-8",
    )
    artifact_report = _inspect_tool_resource_artifacts(trace_dir)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw-sandbox\n  stage2_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    result = {
        "resource_summary": {
            "tool_span_ends": 2,
            "resource_sampled_tool_span_ends": 2,
            "cgroup_sampled_tool_span_ends": 2,
            "launcher_tool_span_ends": 1,
            "launcher_cgroup_tool_span_ends": 1,
            "launcher_stage2_expected_span_ends": 1,
            "launcher_exit_status_span_ends": 1,
            "launcher_tool_resource_span_ends": 1,
            "launcher_stage2_lifecycle_span_ends": 1,
            "launcher_stage2_artifact_envelope_span_ends": 1,
            "launcher_stage2_artifact_refs": [
                {
                    "execution_id": "exec-async",
                    "tool_call_id": "call_async",
                }
            ],
            "launcher_tool_resource_eligible_span_ends": 1,
            "tool_resource_prediction_span_starts": 2,
            "tool_resource_prediction_available_span_starts": 1,
            "clause_bucket_prediction_available_span_starts": 1,
            "continuous_latency_ms_prediction_available_span_starts": 1,
            "continuous_peak_cpu_cores_prediction_available_span_starts": 1,
            "continuous_peak_memory_mb_prediction_available_span_starts": 1,
        },
        "tool_resource_artifacts": artifact_report,
    }

    assert artifact_report["json_file_count"] == 1
    assert artifact_report["artifact_count"] == 1
    assert artifact_report["artifact_envelope_count"] == 1
    assert artifact_report["artifact_identity_count"] == 1
    assert artifact_report["artifact_refs"] == [
        {"execution_id": "exec-async", "tool_call_id": "call_async"}
    ]
    assert artifact_report["collector_healthy_artifact_count"] == 1
    assert artifact_report["healthy_artifact_count"] == 1
    assert artifact_report["kb_eligible_call_count"] == 1
    assert artifact_report["clauses_with_status"] == 1
    assert _required_telemetry_error(config, result) is None

    result["resource_summary"]["launcher_stage2_artifact_refs"] = [
        {"execution_id": "exec-async", "tool_call_id": "wrong-call"}
    ]
    assert "references are inconsistent" in (
        _required_telemetry_error(config, result) or ""
    )
    result["resource_summary"]["launcher_stage2_artifact_refs"] = [
        {"execution_id": "exec-async", "tool_call_id": "call_async"}
    ]

    result["resource_summary"]["launcher_stage2_expected_span_ends"] = 2
    result["resource_summary"]["launcher_exit_status_span_ends"] = 2
    result["resource_summary"]["launcher_tool_resource_span_ends"] = 2
    result["resource_summary"]["launcher_stage2_lifecycle_span_ends"] = 2
    result["resource_summary"]["launcher_stage2_artifact_envelope_span_ends"] = 2
    assert "1/2 executed launcher commands" in (
        _required_telemetry_error(config, result) or ""
    )
    result["resource_summary"]["launcher_stage2_expected_span_ends"] = 1
    result["resource_summary"]["launcher_exit_status_span_ends"] = 1
    result["resource_summary"]["launcher_tool_resource_span_ends"] = 1
    result["resource_summary"]["launcher_stage2_lifecycle_span_ends"] = 1
    result["resource_summary"]["launcher_stage2_artifact_envelope_span_ends"] = 1
    result["resource_summary"]["resource_sampled_tool_span_ends"] = 1
    assert "sampled 1/2" in (_required_telemetry_error(config, result) or "")


def test_trace_prediction_availability_requires_usable_contract_values(tmp_path):
    trace = tmp_path / "trace.jsonl"
    prediction = _stage2_prediction_start()
    payload = prediction["prediction"]["tool_resource"]
    payload["prediction"] = {}
    payload["continuous_predictions"] = {
        "latency_ms": {
            "target": "latency_ms",
            "conditional_p90": "10",
            "scope": "repo",
            "key_kind": "exact_command",
            "evidence_count": 1,
            "fallback_path": [],
            "note": None,
        },
        "peak_cpu_cores": {
            "target": "peak_cpu_cores",
            "conditional_p90": float("inf"),
            "scope": "repo",
            "key_kind": "exact_command",
            "evidence_count": 1,
            "fallback_path": [],
            "note": None,
        },
    }
    trace.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"record_type": "trace_metadata", "task": "task-1"},
                prediction,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    inspected = _inspect_trace(trace, "task-1")

    assert inspected["tool_resource_prediction_span_starts"] == 1
    assert inspected["tool_resource_prediction_available_span_starts"] == 0
    assert inspected["clause_bucket_prediction_available_span_starts"] == 0
    assert inspected["continuous_prediction_available_span_starts"] == 0


def test_required_stage2_accepts_explicit_semantic_rejections_but_not_for_kb(
    tmp_path,
):
    trace_dir = tmp_path / "trace"
    artifact_dir = trace_dir / "tool-resource"
    artifact_dir.mkdir(parents=True)
    parse_id = "call_parse_failed"
    unmatched_id = "call_unmatched"
    parse_artifact = _stage2_artifact(
        parse_id,
        quality="invalid",
        eligible_for_kb=False,
        invalid_reasons=[
            {"kind": "parse_failed", "detail": "shell syntax error"}
        ],
    )
    unmatched_artifact = _stage2_artifact(
        unmatched_id,
        quality="invalid",
        eligible_for_kb=False,
        invalid_reasons=[
            {
                "kind": "unmatched_static_clause",
                "detail": "pip executable was not found",
            }
        ],
        clauses=[_stage2_clause("tail")],
    )
    (artifact_dir / f"{parse_id}.json").write_text(
        json.dumps(parse_artifact), encoding="utf-8"
    )
    (artifact_dir / f"{unmatched_id}.json").write_text(
        json.dumps(unmatched_artifact), encoding="utf-8"
    )
    trace = trace_dir / "trace.jsonl"
    records = [
        {"record_type": "trace_metadata", "task": "task-1"},
        _stage2_prediction_start(),
        _stage2_span_end(parse_id),
        _stage2_prediction_start(),
        _stage2_span_end(unmatched_id),
    ]
    trace.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    inspected = _inspect_trace(trace, "task-1")
    artifact_report = _inspect_tool_resource_artifacts(trace_dir)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw-sandbox\n  stage2_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    result = {
        "resource_summary": _resource_summary([inspected]),
        "tool_resource_artifacts": artifact_report,
    }

    assert inspected["launcher_stage2_lifecycle_span_ends"] == 2
    assert inspected["launcher_stage2_artifact_envelope_span_ends"] == 2
    assert artifact_report["collector_healthy_artifact_count"] == 2
    assert artifact_report["healthy_artifact_count"] == 2
    assert artifact_report["ok_call_count"] == 0
    assert artifact_report["kb_eligible_call_count"] == 0
    assert artifact_report["explicit_semantic_rejection_call_count"] == 2
    assert artifact_report["semantic_rejection_reason_counts"] == {
        "parse_failed": 1,
        "unmatched_static_clause": 1,
    }
    assert len(artifact_report["semantic_rejections"]) == 2
    assert not any(
        "collector/infrastructure is not healthy" in warning
        for warning in artifact_report["warnings"]
    )
    assert _required_telemetry_error(config, result) is None

    parse_artifact["calls"][0]["invalid_reasons"] = [
        {
            "kind": "analysis_failure",
            "detail": "MvdanClientError: adapter is unavailable",
        }
    ]
    (artifact_dir / f"{parse_id}.json").write_text(
        json.dumps(parse_artifact), encoding="utf-8"
    )
    analysis_failure_report = _inspect_tool_resource_artifacts(trace_dir)
    result["tool_resource_artifacts"] = analysis_failure_report
    assert analysis_failure_report["collector_healthy_artifact_count"] == 2
    assert analysis_failure_report["analysis_failure_call_count"] == 1
    assert analysis_failure_report["analysis_failure_reason_counts"] == {
        "analysis_failure": 1,
    }

    assert analysis_failure_report["explicit_semantic_rejection_call_count"] == 1
    assert analysis_failure_report["semantic_rejection_reason_counts"] == {
        "unmatched_static_clause": 1,
    }
    assert len(analysis_failure_report["analysis_failures"]) == 1
    assert "analysis is incomplete" in (
        _required_telemetry_error(config, result) or ""
    )

    parse_artifact["calls"][0]["invalid_reasons"] = []
    (artifact_dir / f"{parse_id}.json").write_text(
        json.dumps(parse_artifact), encoding="utf-8"
    )
    missing_reason_report = _inspect_tool_resource_artifacts(trace_dir)
    result["tool_resource_artifacts"] = missing_reason_report
    assert "lack explicit reasons" in (
        _required_telemetry_error(config, result) or ""
    )

    parse_artifact["calls"][0]["invalid_reasons"] = [
        {"kind": "parse_failed", "detail": "shell syntax error"}
    ]
    parse_artifact["calls"][0]["eligible_for_kb"] = True
    (artifact_dir / f"{parse_id}.json").write_text(
        json.dumps(parse_artifact), encoding="utf-8"
    )
    incorrectly_eligible_report = _inspect_tool_resource_artifacts(trace_dir)
    result["tool_resource_artifacts"] = incorrectly_eligible_report
    assert "KB-withheld contract" in (
        _required_telemetry_error(config, result) or ""
    )

    parse_artifact["calls"][0]["telemetry_quality"] = "mystery"
    parse_artifact["calls"][0]["eligible_for_kb"] = False
    (artifact_dir / f"{parse_id}.json").write_text(
        json.dumps(parse_artifact), encoding="utf-8"
    )
    unknown_quality_report = _inspect_tool_resource_artifacts(trace_dir)
    result["tool_resource_artifacts"] = unknown_quality_report
    assert unknown_quality_report["unaccounted_semantic_call_count"] == 1
    assert _required_telemetry_error(config, result) is not None

    parse_artifact["calls"][0]["telemetry_quality"] = "invalid"
    parse_artifact["calls"][0].pop("eligible_for_kb")
    (artifact_dir / f"{parse_id}.json").write_text(
        json.dumps(parse_artifact), encoding="utf-8"
    )
    missing_eligibility_report = _inspect_tool_resource_artifacts(trace_dir)
    result["tool_resource_artifacts"] = missing_eligibility_report
    assert missing_eligibility_report["unaccounted_semantic_call_count"] == 1
    assert _required_telemetry_error(config, result) is not None


def test_required_stage2_still_fails_real_collector_infrastructure_failure(
    tmp_path,
):
    trace_dir = tmp_path / "trace"
    artifact_dir = trace_dir / "tool-resource"
    artifact_dir.mkdir(parents=True)
    call_id = "call_collector_failed"
    artifact = _stage2_artifact(
        call_id,
        quality="unavailable",
        eligible_for_kb=False,
    )
    artifact["collector"].update(
        {
            "health": "unavailable",
            "state_before_close": "disabled",
            "disabled_reason": "collector attach failed",
            "kprobe_total_hits": 0,
        }
    )
    artifact["calls"][0]["unavailable_reason"] = "collector_attach_failed"
    (artifact_dir / f"{call_id}.json").write_text(
        json.dumps(artifact), encoding="utf-8"
    )
    trace = trace_dir / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"record_type": "trace_metadata", "task": "task-1"},
                _stage2_prediction_start(),
                _stage2_span_end(call_id, status="unavailable"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    inspected = _inspect_trace(trace, "task-1")
    artifact_report = _inspect_tool_resource_artifacts(trace_dir)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw-sandbox\n  stage2_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    error = _required_telemetry_error(
        config,
        {
            "resource_summary": _resource_summary([inspected]),
            "tool_resource_artifacts": artifact_report,
        },
    )

    assert artifact_report["artifact_envelope_count"] == 1
    assert artifact_report["collector_healthy_artifact_count"] == 0
    assert artifact_report["collector_failure_reason_counts"]["collector_health"] == 1
    assert any(
        "collector/infrastructure is not healthy" in warning
        for warning in artifact_report["warnings"]
    )
    assert "collector/infrastructure health is incomplete" in (error or "")


def test_host_sandbox_required_telemetry_requires_predictions(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: host-openclaw-sandbox\n  stage2_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)
    resources = {
        "tool_span_ends": 1,
        "resource_sampled_tool_span_ends": 1,
        "launcher_tool_span_ends": 1,
        "launcher_cgroup_tool_span_ends": 1,
        "launcher_stage2_expected_span_ends": 1,
        "launcher_exit_status_span_ends": 1,
        "launcher_tool_resource_span_ends": 1,
        "launcher_stage2_lifecycle_span_ends": 1,
        "launcher_stage2_artifact_envelope_span_ends": 1,
        "launcher_stage2_artifact_refs": [
            {"execution_id": "exec-1", "tool_call_id": "call-1"}
        ],
        "launcher_tool_resource_eligible_span_ends": 1,
        "tool_resource_prediction_span_starts": 0,
        "tool_resource_prediction_available_span_starts": 0,
        "clause_bucket_prediction_available_span_starts": 0,
        "continuous_latency_ms_prediction_available_span_starts": 0,
        "continuous_peak_cpu_cores_prediction_available_span_starts": 0,
        "continuous_peak_memory_mb_prediction_available_span_starts": 0,
    }
    artifacts = {
        "artifact_count": 1,
        "artifact_envelope_count": 1,
        "artifact_identity_count": 1,
        "artifact_refs": [
            {"execution_id": "exec-1", "tool_call_id": "call-1"}
        ],
        "collector_healthy_artifact_count": 1,
        "healthy_artifact_count": 1,
        "call_count": 1,
        "lifecycle_healthy_call_count": 1,
        "ok_call_count": 1,
        "kb_eligible_call_count": 1,
        "invalid_call_count": 0,
        "unavailable_call_count": 0,
        "non_ok_call_with_reason_count": 0,
        "explicit_semantic_rejection_call_count": 0,
        "unexplained_non_ok_call_count": 0,
        "unaccounted_semantic_call_count": 0,
        "clause_count": 1,
        "clauses_with_status": 1,
    }
    result = {
        "resource_summary": resources,
        "tool_resource_artifacts": artifacts,
    }

    assert "prediction coverage is incomplete" in (
        _required_telemetry_error(config, result) or ""
    )
    resources["tool_resource_prediction_span_starts"] = 1
    assert "produced no usable" in (_required_telemetry_error(config, result) or "")
    resources["tool_resource_prediction_available_span_starts"] = 1
    assert "clause latency-bucket" in (
        _required_telemetry_error(config, result) or ""
    )
    resources["clause_bucket_prediction_available_span_starts"] = 1
    assert "latency_ms,peak_cpu_cores,peak_memory_mb" in (
        _required_telemetry_error(config, result) or ""
    )
    resources["continuous_latency_ms_prediction_available_span_starts"] = 1
    resources["continuous_peak_cpu_cores_prediction_available_span_starts"] = 1
    resources["continuous_peak_memory_mb_prediction_available_span_starts"] = 1
    assert _required_telemetry_error(config, result) is None


def test_container_mode_honors_explicit_stage2_requirement(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  mode: container-openclaw\n  stage2_required: true\n",
        encoding="utf-8",
    )
    config = RunnerConfig.from_yaml(config_path, repo_root=tmp_path)

    assert _required_telemetry_error(
        config,
        {
            "resource_summary": {
                "tool_span_ends": 1,
                "resource_sampled_tool_span_ends": 0,
                "cgroup_sampled_tool_span_ends": 0,
                "launcher_stage2_expected_span_ends": 1,
            },
            "tool_resource_artifacts": {
                "artifact_count": 0,
                "healthy_artifact_count": 0,
                "call_count": 0,
                "ok_call_count": 0,
                "clause_count": 0,
                "clauses_with_status": 0,
            },
        },
    ) == "required resource telemetry is incomplete: sampled 0/1 tool spans"

    assert _required_telemetry_error(
        config,
        {
            "resource_summary": {
                "tool_span_ends": 1,
                "resource_sampled_tool_span_ends": 1,
                "launcher_tool_span_ends": 1,
                "launcher_cgroup_tool_span_ends": 1,
                "launcher_stage2_expected_span_ends": 1,
                "launcher_tool_resource_span_ends": 1,
                "launcher_tool_resource_eligible_span_ends": 1,
            },
            "tool_resource_artifacts": {
                "artifact_count": 1,
                "artifact_envelope_count": 1,
                "collector_healthy_artifact_count": 1,
                "call_count": 1,
                "ok_call_count": 1,
                "kb_eligible_call_count": 1,
                "invalid_call_count": 0,
                "unavailable_call_count": 0,
                "non_ok_call_with_reason_count": 0,
                "explicit_semantic_rejection_call_count": 0,
                "unexplained_non_ok_call_count": 0,
                "unaccounted_semantic_call_count": 0,
                "clause_count": 1,
                "clauses_with_status": 1,
            },
        },
    ) is None


def _runner_config(tmp_path: Path, parallelism: int) -> RunnerConfig:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "runtime:",
                "  mode: host-openclaw-sandbox",
                "  stage2_required: false",
                "batch:",
                f"  parallelism: {parallelism}",
                "output:",
                "  trace_root: traces",
                "  report_path: report.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return RunnerConfig.from_yaml(config_path, repo_root=tmp_path)


def _fake_task_result(task: TaskDef, trace_dir: Path, started: float) -> ContainerResult:
    trace_dir.mkdir(parents=True, exist_ok=True)
    return ContainerResult(
        task_id=task.instance_id,
        image=task.image,
        exit_code=0,
        error=None,
        trace_dir=trace_dir,
        trace_files=[],
        duration_seconds=time.monotonic() - started,
    )


def test_run_batch_parallel_tasks_share_one_sidecar_endpoint(tmp_path, monkeypatch):
    config = _runner_config(tmp_path, parallelism=2)
    tasks = [
        TaskDef("owner__repo-1", "image:1"),
        TaskDef("owner__repo-2", "image:2"),
    ]
    events: list[tuple[str, str | None]] = []

    monkeypatch.setattr(runner, "get_docker_client", lambda _config: object())
    monkeypatch.setattr(
        runner,
        "_prepare_batch_tool_resource_kb",
        lambda shared_kb_dir, _config: shared_kb_dir.mkdir(parents=True),
    )
    monkeypatch.setattr(host_sandbox, "_free_port", lambda: 19090)

    class Sidecar:
        poll = None  # noqa: A003 (return None → process still running)

    monkeypatch.setattr(
        host_sandbox,
        "_start_sidecar",
        lambda **_kwargs: events.append(("sidecar-start", None)) or Sidecar(),
    )
    monkeypatch.setattr(
        host_sandbox,
        "_stop_process",
        lambda _process: events.append(("sidecar-stop", None)),
    )
    monkeypatch.setattr(
        runner,
        "_stop_process",
        lambda _process: events.append(("sidecar-stop", None)),
    )

    def fake_execute_one(**kwargs):
        events.append((kwargs["task"].instance_id, str(kwargs["shared_sidecar_port"])))
        time.sleep(0.02)
        return _fake_task_result(
            kwargs["task"],
            kwargs["trace_dir"],
            time.monotonic() - 0.01,
        )

    monkeypatch.setattr(runner, "_execute_one", fake_execute_one)

    report = runner.run_batch(config, tasks, tmp_path / "bundle")

    endpoints = {entry["sidecar_endpoint"] for entry in report.results}
    assert endpoints == {"http://127.0.0.1:19090"}
    assert {event for event in events if event[0].startswith("owner__")} == {
        ("owner__repo-1", "19090"),
        ("owner__repo-2", "19090"),
    }
    assert events[-1] == ("sidecar-stop", None)


def test_run_batch_parallelism_one_keeps_single_worker_path(tmp_path, monkeypatch):
    config = _runner_config(tmp_path, parallelism=1)
    task = TaskDef("owner__repo-1", "image:1")
    calls: list[str] = []

    monkeypatch.setattr(runner, "get_docker_client", lambda _config: object())
    monkeypatch.setattr(
        runner,
        "_prepare_batch_tool_resource_kb",
        lambda shared_kb_dir, _config: shared_kb_dir.mkdir(parents=True),
    )
    monkeypatch.setattr(host_sandbox, "_free_port", lambda: 19091)
    monkeypatch.setattr(host_sandbox, "_start_sidecar", lambda **_kwargs: object())
    monkeypatch.setattr(host_sandbox, "_stop_process", lambda _process: None)
    monkeypatch.setattr(runner, "_stop_process", lambda _process: None)

    def fake_execute_one(**kwargs):
        calls.append(kwargs["task"].instance_id)
        return _fake_task_result(
            kwargs["task"],
            kwargs["trace_dir"],
            time.monotonic() - 0.01,
        )

    monkeypatch.setattr(runner, "_execute_one", fake_execute_one)

    report = runner.run_batch(config, [task], tmp_path / "bundle")

    assert calls == ["owner__repo-1"]
    assert report.completed == 1
    assert report.failed == 0
    assert report.results[0]["sidecar_endpoint"] == "http://127.0.0.1:19091"


def test_run_batch_keeps_snapshotted_trace_when_task_cleanup_raises(
    tmp_path,
    monkeypatch,
):
    config = _runner_config(tmp_path, parallelism=1)
    task = TaskDef("owner__repo-1", "image:1")

    monkeypatch.setattr(runner, "get_docker_client", lambda _config: object())
    monkeypatch.setattr(
        runner,
        "_prepare_batch_tool_resource_kb",
        lambda shared_kb_dir, _config: shared_kb_dir.mkdir(parents=True),
    )
    monkeypatch.setattr(host_sandbox, "_free_port", lambda: 19092)
    monkeypatch.setattr(host_sandbox, "_start_sidecar", lambda **_kwargs: object())
    monkeypatch.setattr(host_sandbox, "_stop_process", lambda _process: None)
    monkeypatch.setattr(runner, "_stop_process", lambda _process: None)

    def fail_after_trace_snapshot(**kwargs):
        trace_path = kwargs["trace_dir"] / "runtime__session_run.jsonl"
        trace_path.write_text(
            json.dumps(
                {
                    "schema_version": 6,
                    "record_type": "trace_metadata",
                    "trace_format_version": 6,
                    "scaffold": "openclaw",
                    "mode": "collect",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        raise RuntimeError("runtime drain failed")

    monkeypatch.setattr(runner, "_execute_one", fail_after_trace_snapshot)

    report = runner.run_batch(config, [task], tmp_path / "bundle")

    assert report.completed == 0
    assert report.failed == 1
    assert report.results[0]["error"] == "runtime drain failed"
    assert report.results[0]["trace_lines"] == 1
    assert len(report.results[0]["trace_files"]) == 1
