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
