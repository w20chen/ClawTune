from __future__ import annotations

import json
from pathlib import Path

from swe_rebench.docker import ContainerResult

from deep_research_bench.config import DRBConfig
from deep_research_bench.host_runner import _write_drb_task_inputs
from deep_research_bench.runner import (
    _drb_agent_diagnostics,
    _drb_required_telemetry_error,
    _result_dict,
)
from deep_research_bench.task_source import DRBTask


def _config(gate_required: bool = True) -> DRBConfig:
    config = DRBConfig.from_yaml(
        Path(__file__).resolve().parents[1] / "deep_research_bench" / "config.example.yaml"
    )
    config.gate_required = gate_required
    return config


def _result(
    *,
    tool_spans: int = 1,
    sampled: int = 1,
    has_llm: bool = True,
    sandbox_attributed: int = 0,
) -> dict:
    inspection = [
        {
            "tool_span_ends": tool_spans,
            "llm_span_ends": 1 if has_llm else 0,
            "has_llm_span": has_llm,
            "has_tool_span": tool_spans > 0,
            "resource_sampled_tool_span_ends": sampled,
            "shared_sandbox_tool_span_ends": sandbox_attributed,
            "docker_exec_pid_tool_span_ends": 0,
            "cgroup_tool_span_ends": 0,
        }
    ]
    return {
        "resource_summary": {
            "tool_span_ends": tool_spans,
            "resource_sampled_tool_span_ends": sampled,
            "shared_sandbox_tool_span_ends": sandbox_attributed,
            "docker_exec_pid_tool_span_ends": 0,
            "cgroup_tool_span_ends": 0,
        },
        "trace_inspection": inspection,
    }


def test_gate_disabled_never_fails() -> None:
    config = _config(gate_required=False)
    assert _drb_required_telemetry_error(config, _result(tool_spans=0)) is None


def test_gate_fails_on_no_tool_spans() -> None:
    config = _config()
    error = _drb_required_telemetry_error(config, _result(tool_spans=0))
    assert error is not None
    assert "no tool spans" in error


def test_gate_fails_on_incomplete_sampling() -> None:
    config = _config()
    error = _drb_required_telemetry_error(config, _result(tool_spans=3, sampled=2))
    assert error is not None
    assert "sampled 2/3 tool spans" in error


def test_gate_fails_on_no_llm_spans() -> None:
    config = _config()
    error = _drb_required_telemetry_error(config, _result(has_llm=False))
    assert error is not None
    assert "no LLM spans" in error


def test_gate_passes_with_llm_and_sampled_tool_spans() -> None:
    config = _config()
    assert _drb_required_telemetry_error(config, _result()) is None


def test_drb_agent_diagnostics_ignores_missing_patch() -> None:
    # A research task answers a question; a missing code patch is not a failure.
    artifacts = {
        "result_summary.json": {"summary": {"has_patch": False}},
        "agent-stdout.txt": {"preview": ""},
        "agent-stderr.txt": {"preview": ""},
    }
    smoke = {"agent_exit_code": 0, "has_patch": False}
    diagnostics = _drb_agent_diagnostics([], artifacts, smoke)
    assert diagnostics["failure_kind"] is None
    assert diagnostics["failure"] is None


def test_result_dict_inspects_v6_trace(tmp_path) -> None:
    trace = tmp_path / "trace.jsonl"
    lines = [
        {"type": "span_start", "kind": "llm", "name": "model_call"},
        {"type": "span_start", "kind": "tool", "name": "web_search"},
        {
            "type": "span_end",
            "kind": "tool",
            "name": "web_search",
            "status": {"code": "ok"},
            "resources": {
                "scope": "process_tree",
                "attribution_source": "docker-exec-pid",
                "attribution_status": "attributed",
                "sampling_point_count": 5,
                "cpu_time_s": 0.1,
                "rss_peak_bytes": 1024,
                "resource_timeline": [{"t": 0.0}],
            },
            "execution": {"source": "docker-cgroup-diff"},
        },
    ]
    trace.write_text(
        "".join(json.dumps(line) + "\n" for line in lines),
        encoding="utf-8",
    )
    result = ContainerResult(
        task_id="7",
        image="python:3.11-slim",
        exit_code=0,
        error=None,
        trace_dir=tmp_path,
        trace_files=[trace],
        duration_seconds=1.0,
    )
    rendered = _result_dict(_config(), result)
    assert rendered["task_id"] == "7"
    assert rendered["resource_summary"]["tool_span_ends"] == 1
    assert rendered["resource_summary"]["resource_sampled_tool_span_ends"] == 1
    assert rendered["resource_summary"]["docker_exec_pid_tool_span_ends"] == 1
    assert rendered["trace_inspection"][0]["has_llm_span"] is True
    assert rendered["agent_diagnostics"]["failure_kind"] is None


def test_write_drb_task_inputs_writes_manifest_and_reference(tmp_path) -> None:
    task = DRBTask(
        instance_id="7",
        problem_statement="research question",
        reference_answer="the reference article",
        topic="physics",
        difficulty="phd",
        domain="science-technology",
    )
    config = _config()
    workspace = tmp_path / "workspace"
    _write_drb_task_inputs(tmp_path, task, config, workspace)

    prompt = (tmp_path / "agent_prompt.txt").read_text(encoding="utf-8")
    assert "research question" in prompt

    manifest = json.loads((tmp_path / "task_manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_id"] == "7"
    assert manifest["benchmark"] == "deep-research-bench"
    assert manifest["reference_answer_bytes"] == len("the reference article")
    assert manifest["problem_statement_bytes"] == len("research question")
    assert manifest["sandbox_image"] == "python:3.11-slim"

    assert (tmp_path / "reference_answer.txt").read_text(encoding="utf-8") == (
        "the reference article"
    )
    assert (tmp_path / "agent-cwd.txt").read_text(encoding="utf-8").strip() == str(
        workspace
    )


def test_write_drb_task_inputs_skips_empty_reference(tmp_path) -> None:
    task = DRBTask(instance_id="8", problem_statement="q", reference_answer="")
    _write_drb_task_inputs(tmp_path, task, _config(), tmp_path / "workspace")
    assert not (tmp_path / "reference_answer.txt").exists()
