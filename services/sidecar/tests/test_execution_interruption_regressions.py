"""Complete failure labels, interrupted labels, and unavailable trace evidence."""
import json
import importlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor, load_openclaw_trace_observations
from clawtune_sidecar.trace import AgentTestBenchTraceWriter
from tool_resource.runtime_kb import LatencyBuckets
from tool_resource.sdk import DockerCommandObserver
from test_tool_resource_predictor import _runtime_sample, _tool_completion, _tool_request
from test_runtime_abort import register


def predictor_at(path):
    return ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
        buckets=LatencyBuckets((100, 500, 2000)), repo="review", artifact_dir=path)


@pytest.mark.parametrize("exit_code,signal,raw_result,reason,learned", [
    (7, None, "ordinary error", None, True),
    (124, None, "ordinary error", None, True),
    (None, 9, None, None, False),
    (None, 15, None, None, False),
    (124, None, "[timeout]", None, False),
    (0, None, "", "tool_interrupted:cancelled", False),
])
def test_interrupted_execution_retains_partial_clause_labels(
    tmp_path, monkeypatch, exit_code, signal, raw_result, reason, learned,
):
    import tool_resource.sdk as sdk
    monkeypatch.setattr(sdk, "enrich_clause_structure", lambda command, rows: rows)
    now = time.time()
    class Observer:
        telemetry_available = True
        unavailable_reason = None
        def __init__(self, context):
            self.context = context
        def start(self, tool_call_id, command):
            self.call = {"tool_call_id": tool_call_id, "command": command,
                "eligible_for_kb": True, "telemetry_quality": "ok", "clauses": [{
                    "bin": "sleep", "argv": ["sleep", "30"], "ts_start": now - .1,
                    "ts_end": now, "latency_ms": 100, "cpu_ns_cumulative": 10000,
                    "availability": {"latency": "ok", "cpu_time": "ok"},
                    "status": {"state": "signaled" if signal else "exited"},
                }]}
            return object()
        def finish(self, token, **kwargs):
            self.result = kwargs["replay_response"]
            return self.call
        def finalize(self, *, replay_execution):
            self.context.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self.context.artifact_path.write_text(json.dumps({
                "version": 2, "mode": "clause", "replay_execution": replay_execution,
                "cleanup": "ok", "container_id": self.context.container_id,
                "provenance": {"repo": self.context.repo}, "status_model": "call_granular_v1",
                "telemetry_quality": "ok", "collection_validity": "valid",
                "formal_completeness": "complete", "integrity": {"status": "ok"},
                "calls": [self.call], "result": self.result,
            }))
    monkeypatch.setattr(DockerCommandObserver, "attach", classmethod(lambda cls, context: Observer(context)))
    predictor = predictor_at(tmp_path)
    try:
        assert predictor.begin_execution(execution_id="execution", tool_call_id="call",
            command="sleep 30", container_id="a" * 64, gateway_id="g", runtime_id="r")
        summary = predictor.finish_execution(execution_id="execution", exit_code=exit_code,
            signal=signal, raw_result=raw_result, succeeded=False, incomplete_reason=reason)
        assert summary.kb_observations_added == 1
        assert summary.kb_update_error is None
        artifact = json.loads((tmp_path / "execution.json").read_text())
        assert artifact["replay_execution"] == ("failed" if learned else "incomplete")
        assert artifact["result"]["exit_code"] == exit_code
        assert artifact["result"]["signal"] == signal
        predictor.flush_kb_updates(timeout_seconds=5)
        snapshot = tmp_path / "clause-lattice-time-kb.json"
        lattice = json.loads(snapshot.read_text()) if snapshot.exists() else {"observations": [], "pending": []}
        rows = lattice["observations"] + lattice["pending"]
        assert bool(rows)
        # A late/misclassified hook cannot revive labels censored by the
        # authenticated exit, even if its error_type does not mention timeout.
        request = _tool_request("before", "call", "sleep 30")
        predictor.record_tool_started(request)
        completion = _tool_completion("after", "call").model_copy(update={"execution_id": "execution"})
        captured = []
        monkeypatch.setattr(predictor.continuous_kb, "observe_completed_call", captured.append)
        predictor.observe_completion(completion, _runtime_sample("after", "call"))
        assert captured[0].censored is (not learned)
        writer = AgentTestBenchTraceWriter(tmp_path / "trace")
        writer.record_tool_started(request)
        writer.record_tool_resource_telemetry("execution", summary)
        writer.record_tool(completion, _runtime_sample("after", "call"))
        writer.close()
        trace_path = next((tmp_path / "trace").glob("*.jsonl"))
        loaded = load_openclaw_trace_observations(trace_path, repo="review")
        assert len(loaded.completed_calls) == 1
        assert loaded.completed_calls[0].censored is (not learned)
    finally:
        predictor.close()


@pytest.mark.parametrize("signal", [None, 9])
def test_sdk_start_failure_keeps_trace_writable_and_drainable(tmp_path, monkeypatch, signal):
    predictor = predictor_at(tmp_path / "kb")
    writer = AgentTestBenchTraceWriter(tmp_path / "trace")
    def fail(*args, **kwargs):
        raise RuntimeError("controlled SDK start failure")
    monkeypatch.setattr(predictor._sdk, "start_command", fail)
    try:
        assert not predictor.begin_execution(execution_id="missing", tool_call_id="call",
            command="echo ok", container_id="a" * 64)
        summary = predictor.finish_execution(execution_id="missing", exit_code=None if signal else 0, signal=signal)
        assert summary.replay_execution == ("incomplete" if signal else "completed")
        assert summary.artifact_path is None
        assert summary.status == "unavailable"
        assert "controlled SDK start failure" in summary.unavailable_reason
        writer.record_tool_resource_telemetry("missing", summary, gateway_id="g", runtime_id="r")
        assert writer.flush()
        predictor.flush_kb_updates(timeout_seconds=5)
        assert not predictor.has_active_executions()
    finally:
        writer.close()
        predictor.close()
    rows = [json.loads(line) for path in (tmp_path / "trace").glob("*.jsonl")
            for line in path.read_text().splitlines()]
    event = next(row for row in rows if row.get("event_type") == "execution_telemetry")
    assert event["payload"]["artifact_path"] is None
    assert "artifact" not in event


def test_old_failed_artifact_retains_signaled_partial_clause(monkeypatch):
    import tool_resource.sdk as sdk
    monkeypatch.setattr(sdk, "enrich_clause_structure", lambda command, rows: rows)
    row = {"bin": "sleep", "argv": ["sleep", "30"], "ts_start": 1, "ts_end": 2,
           "latency_ms": 1000, "availability": {"latency": "ok"},
           "status": {"state": "signaled", "signal": 9}}
    call = {"command": "sleep 30", "clauses": [row], "eligible_for_kb": True}
    assert sdk._observations_from_call("review", call, require_timestamps=True)[0].censored
    row["status"] = {"state": "exited", "exit_code": 7, "signal": None}
    assert len(sdk._observations_from_call("review", call, require_timestamps=True)) == 1


@pytest.mark.parametrize("error,raw,expected", [
    ("cancelled", None, "tool_interrupted:cancelled"),
    (None, {"details": {"exitReason": "overall-timeout"}}, "tool_timeout"),
    ("exit_code_7", None, None),
])
def test_completion_preserves_exit_evidence_and_marks_explicit_interruptions(tmp_path, monkeypatch, error, raw, expected):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "trace", tool_resource_ebpf_required=False))
    finished = []
    monkeypatch.setattr(state.predictor, "finish_execution", lambda **kwargs: finished.append(kwargs))
    with TestClient(create_app(state)) as client:
        token = register(client, "execution")
        assert client.post("/v2/executions/execution/exited", json={
            "update_token": token, "exit_code": 7, "signal": None}).status_code == 200
        completion = _tool_completion("done", "call-execution").model_copy(update={
            "execution_id": "execution", "runtime_id": "task-a", "gateway_id": "swe-rebench",
            "run_id": "run", "session_id": None, "agent_id": None,
            "error_type": error, "raw_result": raw, "succeeded": False,
        })
        response = client.post("/v1/events/tool-completed", json=completion.model_dump(mode="json"))
        assert response.status_code == 200, response.text
    assert len(finished) == 1
    assert finished[0]["exit_code"] == 7
    assert finished[0]["signal"] is None
    assert finished[0].get("incomplete_reason") == expected


@pytest.mark.parametrize("raises", [True, False])
def test_interruption_censor_survives_collector_finish_failure_and_trace_reload(tmp_path, monkeypatch, raises):
    predictor = predictor_at(tmp_path / "kb")
    run = SimpleNamespace(tool_call_id="call", _observer=SimpleNamespace(
        context=SimpleNamespace(artifact_path=tmp_path / "missing.json")))
    predictor._runs_by_execution_id["execution"] = run
    def fail(*args, **kwargs):
        if raises:
            raise RuntimeError("controlled finalize failure")
        return SimpleNamespace(kb_observations_added=0, telemetry_artifact=None,
            call_telemetry=None, kb_update_error="controlled finalize failure")
    monkeypatch.setattr(predictor._sdk, "finish_command", fail)
    try:
        summary = predictor.finish_execution(execution_id="execution", exit_code=None, signal=9)
        assert summary.replay_execution == "incomplete"
        assert summary.artifact_path is None
        assert "controlled finalize failure" in summary.kb_update_error
        request = _tool_request("before", "call", "sleep 30")
        completion = _tool_completion("after", "call").model_copy(update={
            "execution_id": "execution", "succeeded": False, "error_type": "exit_code_137"})
        sample = _runtime_sample("after", "call")
        predictor.record_tool_started(request)
        captured = []
        monkeypatch.setattr(predictor.continuous_kb, "observe_completed_call", captured.append)
        predictor.observe_completion(completion, sample)
        assert captured[0].censored
        writer = AgentTestBenchTraceWriter(tmp_path / "trace")
        writer.record_tool_started(request)
        writer.record_tool_resource_telemetry("execution", summary)
        writer.record_tool(completion, sample)
        writer.close()
        loaded = load_openclaw_trace_observations(next((tmp_path / "trace").glob("*.jsonl")), repo="review")
        assert loaded.completed_calls[0].censored
    finally:
        predictor.close()


@pytest.mark.parametrize("before,late,exit_code,timeout,learned", [
    ("matching", True, 124, True, False),
    ("matching", True, 0, False, False),
    ("matching", True, 7, False, False),
    ("matching", False, 7, False, True),
    ("matching", False, 124, False, True),
    ("none", True, 0, False, True),
    ("none", True, 7, False, True),
    ("foreign", True, 7, False, True),
])
def test_fallback_marks_missing_completion_but_retains_observations(
    tmp_path, monkeypatch, before, late, exit_code, timeout, learned,
):
    app_module = importlib.import_module("clawtune_sidecar.api.app")
    monkeypatch.setattr(app_module, "_EBPF_COMPLETION_GRACE_SECONDS", .1 if late else 30)
    monkeypatch.setattr("tool_resource.sdk.enrich_clause_structure", lambda command, rows: rows)

    class Observer:
        telemetry_available = True
        unavailable_reason = None
        def __init__(self, context):
            self.context = context
        def start(self, tool_call_id, command):
            now = time.time()
            self.call = {"tool_call_id": tool_call_id, "command": command, "eligible_for_kb": True,
                "telemetry_quality": "ok", "clauses": [{"bin": "sleep", "argv": ["sleep", "30"],
                "ts_start": now-.1, "ts_end": now, "latency_ms": 100,
                "availability": {"latency": "ok"},
                "status": {"state": "exited", "exit_code": exit_code, "signal": None}}]}
            return object()
        def finish(self, token, **kwargs):
            self.result = kwargs["replay_response"]
            return self.call
        def finalize(self, *, replay_execution):
            self.context.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self.context.artifact_path.write_text(json.dumps({"version": 2, "mode": "clause",
                "replay_execution": replay_execution, "cleanup": "ok", "container_id": self.context.container_id,
                "provenance": {"repo": self.context.repo}, "status_model": "call_granular_v1",
                "telemetry_quality": "ok", "collection_validity": "valid", "formal_completeness": "complete",
                "integrity": {"status": "ok"}, "calls": [self.call], "result": self.result}))

    monkeypatch.setattr(DockerCommandObserver, "attach", classmethod(lambda cls, context: Observer(context)))
    state = build_state(SidecarConfig(trace_dir=tmp_path / "trace", tool_resource_ebpf_required=False))
    state.predictor.close()
    state.predictor = predictor_at(tmp_path / "kb")
    monkeypatch.setattr(state.tool_monitor, "prepare", lambda: None)
    captured = []
    monkeypatch.setattr(state.predictor.continuous_kb, "observe_completed_call", captured.append)
    owner = {"gateway_id": "swe-rebench", "runtime_id": "task-a", "run_id": "run",
             "session_id": None, "agent_id": None}
    with TestClient(create_app(state)) as client:
        decision = None
        if before != "none":
            request = _tool_request("before", "call-execution", "sleep 30").model_copy(update={
                **owner, "runtime_id": "foreign-task" if before == "foreign" else "task-a"})
            response = client.post("/v1/decisions/tool", json=request.model_dump(mode="json"))
            assert response.status_code == 200, response.text
            decision = response.json()
        token = register(client, "execution")
        assert state.predictor.begin_execution(execution_id="execution", tool_call_id="call-execution",
            command="sleep 30", container_id="a" * 64, gateway_id="swe-rebench", runtime_id="task-a")
        response = client.post("/v2/executions/execution/exited", json={
            "update_token": token, "exit_code": exit_code, "signal": None})
        assert response.status_code == 200, response.text
        if late:
            deadline = time.monotonic() + 5
            while state.predictor.execution_active("execution"):
                assert time.monotonic() < deadline
                time.sleep(.01)
            # Wait for the worker to publish its summary after removing its run.
            while state.predictor.execution_telemetry("execution").replay_execution is None:
                assert time.monotonic() < deadline
                time.sleep(.01)
        if before == "matching":
            event = _tool_completion("after", "call-execution").model_copy(update={
                **owner, "execution_id": "execution", "succeeded": exit_code == 0,
                "error_type": None if timeout or exit_code == 0 else f"exit_code_{exit_code}",
                "raw_result": ({"details": {"exitReason": "overall-timeout"}, "exit_code": exit_code}
                               if timeout else {"exit_code": exit_code}),
                "decision_id": decision["decision_id"], "lease_id": decision["lease_id"]})
            response = client.post("/v1/events/tool-completed", json=event.model_dump(mode="json"))
            assert response.status_code == 200, response.text
        response = client.post("/v1/gateways/swe-rebench/runtimes/task-a/drain?timeout_seconds=5&flush_kb=true")
        assert response.status_code == 200 and response.json()["drained"], response.text
        summary = state.predictor.execution_telemetry("execution")
        assert summary.kb_observations_added == 1
        assert summary.kb_update_error is None
        if not learned:
            assert summary.replay_execution == "incomplete"
            assert summary.unavailable_reason == "completion_hook_missing"
        else:
            assert summary.replay_execution == ("completed" if exit_code == 0 else "failed")
        artifact = json.loads(Path(summary.artifact_path).read_text())
        assert artifact["replay_execution"] == summary.replay_execution
        if before != "matching":
            assert summary.call_telemetry is None
            assert "execution" in state.predictor._completed_execution_ids
            assert "execution" not in state.predictor._execution_owners
            assert "execution" not in state.predictor._completion_inputs
            assert "execution" not in state.predictor._pending_memory_targets
        snapshot = tmp_path / "kb/clause-lattice-time-kb.json"
        lattice = json.loads(snapshot.read_text()) if snapshot.exists() else {}
        assert bool(lattice.get("observations") or lattice.get("pending"))
        if before == "matching":
            assert len(captured) == 1 and captured[0].censored is (not learned)
            from tool_resource.runtime_kb import _target_values
            assert _target_values(captured[0])["latency_ms"] > 0
    if before == "matching":
        loaded = [call for path in (tmp_path / "trace").glob("*.jsonl")
                  for call in load_openclaw_trace_observations(path, repo="review").completed_calls]
        assert len(loaded) == 1 and loaded[0].censored is (not learned)
