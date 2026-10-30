"""Completed history must neither retain payloads nor evict pending measurements."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from clawtune_sidecar.predictors.tool_resource import ExecutionTelemetrySummary
from test_execution_interruption_regressions import predictor_at
from test_tool_resource_predictor import _runtime_sample, _tool_completion


def install_pending(predictor, key="execution"):
    summary = ExecutionTelemetrySummary(
        key, "call", "/saved/execution.json", True, "ok",
        kb_observations_added=2, kb_update_error="retained error",
        replay_execution="incomplete", confirmed_timeout=True,
        timeout_duration_lower_bound_ms=3000,
        call_telemetry={"clauses": [{"argv": ["sleep", "3"]}]},
        artifact_summary={"replay_execution": "incomplete", "collector": {"large": [1] * 100}},
    )
    predictor._telemetry_by_execution_id[key] = summary
    predictor._execution_owners[key] = ("g", "r")
    predictor._completion_inputs[key] = (("g", "r"), {"calls": []})
    return summary


def test_processing_failure_preserves_inputs_until_successful_retry(tmp_path):
    predictor = predictor_at(tmp_path)
    event = _tool_completion("after", "call").model_copy(update={"execution_id": "execution"})
    sample = _runtime_sample("after", "call")
    original = install_pending(predictor)
    pending = predictor._completion_inputs["execution"]
    try:
        with patch.object(predictor.continuous_kb, "observe_completed_call", side_effect=RuntimeError("retry")):
            with pytest.raises(RuntimeError, match="retry"):
                predictor.observe_completion(event, sample)
        assert predictor._completion_inputs["execution"] is pending
        assert predictor.execution_telemetry("execution") is original
        with patch.object(predictor.continuous_kb, "observe_completed_call"):
            predictor.observe_completion(event, sample)
        assert "execution" not in predictor._completion_inputs
        assert predictor.execution_telemetry("execution").call_telemetry is None
    finally:
        predictor.close()


def test_retirement_preserves_status_duplicates_and_already_returned_trace_payload(tmp_path):
    predictor = predictor_at(tmp_path)
    original = install_pending(predictor)
    predictor._memory_targets_committed.add("execution")
    try:
        predictor._retire_execution_telemetry("execution")
        assert predictor.execution_telemetry("execution") == replace(
            original, call_telemetry=None, artifact_summary={"replay_execution": "incomplete"})
        # The trace writer owns the original object; retirement must not mutate it.
        assert original.call_telemetry["clauses"][0]["argv"] == ["sleep", "3"]
        assert original.artifact_summary["collector"]
        assert "execution" in predictor._memory_targets_committed
        with patch.object(predictor._sdk, "start_command", side_effect=AssertionError("duplicate start")):
            assert predictor.begin_execution(execution_id="execution", tool_call_id="call", container_id="c", command="sleep 3")
        retry = predictor.finish_execution(execution_id="execution", exit_code=0, signal=None,
                                          confirmed_timeout=True, timeout_duration_lower_bound_ms=4000)
        assert retry.confirmed_timeout and retry.timeout_duration_lower_bound_ms == 4000
        assert retry.replay_execution == "incomplete" and retry.call_telemetry is None
        assert len(predictor._completed_execution_ids) == 1
    finally:
        predictor.close()


@pytest.mark.parametrize("reason", ["active", "finishing", "memory_pending"])
def test_retirement_waits_for_remaining_work(tmp_path, reason):
    predictor = predictor_at(tmp_path)
    original = install_pending(predictor)
    try:
        if reason == "active":
            predictor._runs_by_execution_id["execution"] = object()
        elif reason == "finishing":
            original = replace(original, replay_execution=None)
            predictor._telemetry_by_execution_id["execution"] = original
        else:
            predictor._pending_memory_targets["execution"] = (("g", "r"), ())
        predictor._retire_execution_telemetry("execution")
        assert predictor.execution_telemetry("execution") is original
        assert "execution" in predictor._completion_inputs
        assert not predictor._completed_execution_ids
    finally:
        predictor._runs_by_execution_id.clear()
        predictor.close()


def test_history_pressure_does_not_drop_unfinished_calls_or_their_dedup_state(tmp_path):
    predictor = predictor_at(tmp_path)
    original = install_pending(predictor, "pending")
    predictor._memory_targets_committed.add("pending")
    try:
        for i in range(10_002):
            key = str(i)
            install_pending(predictor, key)
            predictor._memory_targets_committed.add(key)
            predictor._retire_execution_telemetry(key)
        assert len(predictor._completed_execution_ids) == 10_000
        assert predictor.execution_telemetry("pending") is original
        assert list(predictor._completion_inputs) == ["pending"]
        assert "pending" in predictor._memory_targets_committed
        assert "0" not in predictor._memory_targets_committed
        assert predictor.execution_telemetry("0") is None
    finally:
        predictor.close()


@pytest.mark.parametrize("start_failure", [False, True])
def test_runtime_close_retires_unavailable_executions_only_for_its_owner(tmp_path, start_failure):
    predictor = predictor_at(tmp_path)
    try:
        with patch.object(predictor._sdk, "start_command", side_effect=RuntimeError("cannot start")):
            for key, gateway in (("one", "g"), ("two", "other")):
                assert not predictor.begin_execution(
                    execution_id=key, tool_call_id=key, command="sleep 1",
                    container_id="container" if start_failure else None,
                    gateway_id=gateway, runtime_id="same-runtime")
        predictor.finalize_runtime("same-runtime", "g")
        assert list(predictor._completed_execution_ids) == ["one"]
        assert predictor._execution_owners == {"two": ("other", "same-runtime")}
        assert predictor.execution_telemetry("one").unavailable_reason
        assert not predictor.execution_telemetry("one").started
    finally:
        predictor.close()
