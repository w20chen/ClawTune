from __future__ import annotations

import json

from swe_rebench.config import RunnerConfig
from swe_rebench.runner import (
    _inspect_tool_resource_artifacts,
    _inspect_trace,
    _required_telemetry_error,
    _resource_summary,
)


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
    (artifact_dir / "call_async.json").write_text(
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
        },
        "tool_resource_artifacts": artifact_report,
    }

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
