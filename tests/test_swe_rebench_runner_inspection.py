from __future__ import annotations

import json

from swe_rebench.runner import _inspect_trace, _resource_summary


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
