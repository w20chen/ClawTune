from __future__ import annotations

import json

from swe_rebench.config import RunnerConfig
from swe_rebench.runner import (
    _agent_diagnostics,
    _inspect_tool_resource_artifacts,
    _inspect_trace,
    _required_telemetry_error,
    _resource_summary,
)


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
                        "latency_ms": {"conditional_p90": 100.0},
                        "peak_cpu_cores": {"conditional_p90": None},
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
    assert "Stage-2 tool-resource telemetry is unavailable for some launcher tool spans" in inspected["warnings"]
    assert summary["launcher_tool_resource_unavailable_span_ends"] == 1
    assert summary["tool_resource_prediction_available_ratio"] == 1.0


def test_required_telemetry_audits_all_tool_samples_and_async_artifacts(tmp_path):
    trace_dir = tmp_path / "trace"
    artifact_dir = trace_dir / "tool-resource"
    artifact_dir.mkdir(parents=True)
    # Fallback execution IDs are exec-<uuid>, not necessarily call_<id>.
    # The audit must discover artifacts by schema rather than filename prefix.
    (artifact_dir / "exec-async.json").write_text(
        json.dumps(
            {
                "schema": "clause_telemetry_v2",
                "version": 2,
                "mode": "clause",
                "status_model": "call_granular_v1",
                "telemetry_quality": "ok",
                "collection_validity": "valid",
                "cleanup": "ok",
                "collector": {"health": "healthy"},
                "calls": [
                    {
                        "tool_call_id": "call_async",
                        "command": "printf hi",
                        "telemetry_quality": "ok",
                        "eligible_for_kb": True,
                        "clauses": [
                            {
                                "bin": "printf",
                                "status": {
                                    "state": "exited",
                                    "exit_code": 0,
                                    "signal": None,
                                    "succeeded": True,
                                    "reason": None,
                                    "source": "root_exec_chain_terminal",
                                },
                            }
                        ],
                        "no_runtime_exec": [],
                    }
                ],
            }
        ),
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
            "launcher_stage2_expected_span_ends": 1,
            "tool_resource_prediction_span_starts": 2,
            "tool_resource_prediction_available_span_starts": 1,
        },
        "tool_resource_artifacts": artifact_report,
    }

    assert artifact_report["json_file_count"] == 2
    assert artifact_report["artifact_count"] == 1
    assert artifact_report["healthy_artifact_count"] == 1
    assert artifact_report["clauses_with_status"] == 1
    assert _required_telemetry_error(config, result) is None

    result["resource_summary"]["launcher_stage2_expected_span_ends"] = 2
    assert "1/2 executed launcher commands" in (
        _required_telemetry_error(config, result) or ""
    )
    result["resource_summary"]["launcher_stage2_expected_span_ends"] = 1
    result["resource_summary"]["resource_sampled_tool_span_ends"] = 1
    assert "sampled 1/2" in (_required_telemetry_error(config, result) or "")


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
        "tool_resource_prediction_span_starts": 0,
        "tool_resource_prediction_available_span_starts": 0,
    }
    artifacts = {
        "artifact_count": 1,
        "healthy_artifact_count": 1,
        "call_count": 1,
        "ok_call_count": 1,
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
