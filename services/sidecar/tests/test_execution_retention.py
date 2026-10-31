"""Completed history must neither retain payloads nor evict pending measurements."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from clawtune_sidecar.predictors.tool_resource import ExecutionTelemetrySummary
from test_execution_interruption_regressions import predictor_at
from test_tool_resource_predictor import _runtime_sample, _tool_completion
from tool_resource.runtime_kb import ClauseObservation


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


def stub_collector(predictor, monkeypatch, *, rows=()):
    call = {"command": "sleep 1", "clauses": [{"bin": "sleep", "argv": ["sleep", "1"],
            "ts_start": 1., "ts_end": 2., "latency_ms": 1000}]}
    result = SimpleNamespace(kb_observations_added=len(rows), kb_observations=rows,
        telemetry_artifact={"version": 2, "mode": "clause", "calls": [call]},
        call_telemetry=call, kb_update_error=None)
    monkeypatch.setattr(predictor._sdk, "start_command", lambda context, call_id, command:
        SimpleNamespace(tool_call_id=call_id, _observer=SimpleNamespace(context=context)))
    monkeypatch.setattr(predictor._sdk, "finish_command", lambda *args, **kwargs: result)
    return result


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("completion_expected", [False, True])
def test_explicit_no_hook_finish_retires_only_its_own_inputs(tmp_path, monkeypatch, frozen, completion_expected):
    predictor = predictor_at(tmp_path)
    predictor.frozen = frozen
    row = ClauseObservation("review", "sleep", ("sleep", "1"), 1, 2,
                            latency_ms=1000, cpu_ns_cumulative=500000000)
    stub_collector(predictor, monkeypatch, rows=(row,))
    other = install_pending(predictor, "other")
    other_rows = (("other-gateway", "same-runtime"), (replace(row, ts_start=3, ts_end=4),))
    predictor._pending_memory_targets["other"] = other_rows
    try:
        assert predictor.begin_execution(execution_id="direct", tool_call_id="direct-call",
            container_id="container", command="sleep 1", gateway_id="g", runtime_id="same-runtime")
        # The handoff path only retains validated artifacts that exist on disk.
        (tmp_path / "direct.json").write_text('{"version": 2, "mode": "clause", "calls": []}')
        returned = predictor.finish_execution(execution_id="direct", exit_code=0, signal=None,
                                             completion_expected=completion_expected)
        stored = predictor.execution_telemetry("direct")
        assert returned.call_telemetry is not None  # trace serialization still owns full data
        assert (stored.call_telemetry is not None) is completion_expected
        assert ("direct" in predictor._execution_owners) is completion_expected
        assert ("direct" in predictor._completion_inputs) is completion_expected
        assert ("direct" in predictor._pending_memory_targets) is (completion_expected and not frozen)
        assert ("direct" in predictor._completed_execution_ids) is (not completion_expected)
        assert predictor.execution_telemetry("other") is other
        assert predictor._pending_memory_targets["other"] is other_rows
        if not completion_expected:
            predictor.finish_execution(execution_id="direct", exit_code=0, signal=None,
                                       completion_expected=False)
            observations = predictor.kb.to_json_obj()
            rows = observations["pending"]
            assert len(rows) == (0 if frozen else 2)
            if not frozen:
                assert [r["cpu_ns_cumulative"] for r in rows if r["cpu_ns_cumulative"] is not None] == [500000000]
                memory = [r for r in rows if r["memory_total_peak_bytes"] is not None]
                assert len(memory) == 1 and memory[0]["memory_total_peak_bytes"] == 0
                assert "memory_total_peak_bytes" in memory[0]["imputed_targets"]
    finally:
        predictor.close()


@pytest.mark.parametrize("failure", ["start", "finish"])
def test_no_hook_failed_collector_is_retired(tmp_path, monkeypatch, failure):
    predictor = predictor_at(tmp_path)
    stub_collector(predictor, monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError("collector failed")
    monkeypatch.setattr(predictor._sdk, failure + "_command", fail)
    try:
        predictor.begin_execution(execution_id="direct", tool_call_id="call", container_id="c", command="sleep 1")
        returned = predictor.finish_execution(execution_id="direct", exit_code=0, signal=None,
                                             completion_expected=False)
        assert returned.status == "unavailable" and "failed" in returned.unavailable_reason
        assert list(predictor._completed_execution_ids) == ["direct"]
        assert not predictor._execution_owners and not predictor._completion_inputs
    finally:
        predictor.close()


def test_direct_execution_history_is_bounded_without_evicting_pending_hook(tmp_path, monkeypatch):
    predictor = predictor_at(tmp_path)
    predictor.frozen = True
    stub_collector(predictor, monkeypatch)
    pending = install_pending(predictor, "pending-hook")
    try:
        for i in range(10_005):
            key = str(i)
            assert predictor.begin_execution(execution_id=key, tool_call_id=key,
                container_id="container", command="sleep 1")
            predictor.finish_execution(execution_id=key, exit_code=0, signal=None,
                                       completion_expected=False)
        assert len(predictor._completed_execution_ids) == 10_000
        assert len(predictor._telemetry_by_execution_id) == 10_001
        assert predictor.execution_telemetry("0") is None
        assert predictor.execution_telemetry("pending-hook") is pending
        assert list(predictor._execution_owners) == ["pending-hook"]
        assert list(predictor._completion_inputs) == ["pending-hook"]
        assert not predictor._runs_by_execution_id
    finally:
        predictor.close()
