"""Prediction ownership, shutdown fencing, and independent learning evidence."""
import asyncio
import copy
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_command_rule_generation import generated_rule
from test_tool_resource_predictor import _runtime_sample, _tool_completion, _tool_request

from clawtune_kb import FILES, create_seed
from clawtune_kb.store import StateStore
from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.identity import task_instance_key
from clawtune_sidecar.identity import call_key
from clawtune_sidecar.predictors.tool_resource import (
    ToolResourcePredictor,
    _KnowledgeBaseWriteCoordinator,
)
from command_parser import CommandFeatureService, ParseContext
from command_parser.generation import GeneratorConfig, RuleGenerator
from command_parser.registry import RuleRegistry
from execution_context.duration import SNAPSHOT_NAME, DurationContextKB, independent_tasks
from tool_resource.runtime_kb import LatencyBuckets


def request(ident="call", command="python -m pip install example", **owner):
    return _tool_request(ident, ident, command).model_copy(update={
        "gateway_id": "gateway", "runtime_id": "runtime", **owner})


@pytest.fixture
def predictor():
    value = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(), ebpf_trace_paths=(), buckets=LatencyBuckets((100, 500, 2000)))
    yield value
    value.close()


def assert_released(predictor):
    assert not predictor._starts
    assert not predictor._context_pending
    assert not predictor._feature_call_keys
    assert not predictor.command_features._calls
    for history in predictor._context_histories.values():
        assert not history.pending
        assert not history.evidence.active
        assert not history.evidence.started


@pytest.mark.parametrize("failure", ["block", "policy_exception", "prediction_exception"])
def test_decision_releases_all_prediction_state(tmp_path, monkeypatch, failure):
    state = build_state(SidecarConfig(trace_dir=tmp_path, policy="concurrency", admission_wait_ms=1))
    snapshots = []
    original = state.predictor.predict

    def predict(req):
        result = original(req)
        snapshots.append(state.predictor._context_pending[call_key(req)][2])
        if failure == "prediction_exception":
            raise ValueError("injected prediction failure")
        return result

    monkeypatch.setattr(state.predictor, "predict", predict)

    async def reject(*args, **kwargs):
        if failure == "policy_exception":
            raise ValueError("injected policy failure")

    monkeypatch.setattr(state.leases, "acquire", reject)
    with TestClient(create_app(state), raise_server_exceptions=False) as client:
        for req in (request(), request("probe", "python --version")):
            response = client.post("/v1/decisions/tool", json=req.model_dump(mode="json"))
            if failure == "block":
                assert response.json()["action"] == "block"
            else:
                assert response.status_code == 500
            assert_released(state.predictor)
        assert snapshots[-1]["snapshots"][0]["history_blocked"] is False
        assert not state.predictor.duration_context_kb.samples


def test_runtime_abort_releases_only_its_owner(tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    with TestClient(create_app(state)) as client:
        own, foreign = request(), request(gateway_id="other")
        for req in (own, foreign):
            assert client.post("/v1/decisions/tool", json=req.model_dump(mode="json")).json()["action"] == "allow"
        body = {"reason": "task_timeout", "agent_stopped": True, "sandbox_cleaned": True}
        for _ in range(2):
            assert client.post("/v1/gateways/gateway/runtimes/runtime/abort", json=body).status_code == 200
        assert set(state.predictor._context_pending) == {call_key(foreign)}
        assert all(scope[0] == "other" for scope in state.predictor._context_histories)
        assert not state.predictor.duration_context_kb.samples


@pytest.mark.parametrize("failed", [False, True])
def test_completion_releases_state_even_on_learning_error(predictor, monkeypatch, failed):
    req = request()
    predictor.record_tool_started(req)
    predictor.predict(req)
    if failed:
        def fail(*args, **kwargs):
            raise ValueError("injected learning failure")
        monkeypatch.setattr("clawtune_sidecar.predictors.tool_resource.completed_call_from_completion", fail)
    event = _tool_completion("done", "call").model_copy(update={"gateway_id": "gateway", "runtime_id": "runtime"})
    if failed:
        with pytest.raises(ValueError, match="injected"):
            predictor.observe_completion(event, _runtime_sample("done", "call"))
    else:
        predictor.observe_completion(event, _runtime_sample("done", "call"))
    predictor.finalize_call(req)
    predictor.finalize_call(req)
    assert_released(predictor)


def test_cancelled_decision_waits_for_prediction_thread(tmp_path, monkeypatch):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    started, release = threading.Event(), threading.Event()
    original = state.predictor.predict

    def slow(req):
        started.set()
        assert release.wait(5)
        return original(req)

    monkeypatch.setattr(state.predictor, "predict", slow)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(state)), base_url="http://test") as client:
            response = asyncio.create_task(client.post("/v1/decisions/tool", json=request().model_dump(mode="json")))
            assert await asyncio.to_thread(started.wait, 3)
            task = next(iter(state._decision_tasks.values()))
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await response
            assert_released(state.predictor)
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        state.predictor.close()
        state.tool_monitor.stop()
        state.trace_writer.close()


def test_close_timeout_retains_writer_lock_and_fences_updates(tmp_path, predictor):
    owner = StateStore(tmp_path).__enter__()
    predictor._state_store = owner
    started, release = threading.Event(), threading.Event()

    def write(rows):
        started.set()
        assert release.wait(5)

    coordinator = _KnowledgeBaseWriteCoordinator(write, batch_window_s=0)
    predictor._kb_writes = coordinator
    coordinator.enqueue()
    assert started.wait(3)
    try:
        with pytest.raises(TimeoutError):
            predictor.close(timeout_seconds=.01)
        assert coordinator.pending()
        with pytest.raises(OSError):
            StateStore(tmp_path).__enter__()
        with pytest.raises(RuntimeError, match="closing"):
            predictor.predict(request())
        with pytest.raises(RuntimeError, match="closing"):
            coordinator.enqueue()
    finally:
        release.set()
        predictor.close(timeout_seconds=3)
    assert not coordinator._worker.is_alive()
    with StateStore(tmp_path):
        pass
    predictor.close()


@pytest.mark.parametrize("error", [OSError, TimeoutError])
def test_close_persistence_failure_can_be_retried(tmp_path, predictor, error):
    predictor._state_store = StateStore(tmp_path).__enter__()
    attempts = []

    def write(rows):
        attempts.append(1)
        if len(attempts) == 1:
            raise error("disk unavailable")

    predictor._kb_writes = _KnowledgeBaseWriteCoordinator(write)
    with pytest.raises(OSError, match="disk unavailable"):
        predictor.close()
    with pytest.raises(OSError):
        StateStore(tmp_path).__enter__()
    predictor.close()
    assert len(attempts) == 2
    with StateStore(tmp_path):
        pass


def test_close_waits_for_admitted_operation_before_stopping_writer(tmp_path, predictor, monkeypatch):
    predictor._state_store = StateStore(tmp_path).__enter__()
    # This test concerns ownership, not the StateStore checkpoint format.
    monkeypatch.setattr(predictor._state_store, "checkpoint", lambda: None)
    started, release = threading.Event(), threading.Event()
    original = predictor._predict
    errors = []

    def slow(req):
        started.set()
        assert release.wait(5)
        return original(req)

    def run():
        try:
            predictor.predict(request())
        except BaseException as exc:  # noqa: BLE001 - surface worker failures in the test thread
            errors.append(exc)

    monkeypatch.setattr(predictor, "_predict", slow)
    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(3)
    try:
        with pytest.raises(TimeoutError, match="active predictor operations"):
            predictor.close(timeout_seconds=.01)
        with pytest.raises(OSError):
            StateStore(tmp_path).__enter__()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    predictor.close()
    assert_released(predictor)


@pytest.mark.parametrize("gateway", [None, "gateway"])
def test_runtime_scope_deletion_releases_prediction_history(tmp_path, gateway):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    with TestClient(create_app(state)) as client:
        req = request(gateway_id=gateway)
        assert client.post("/v1/decisions/tool", json=req.model_dump(mode="json")).status_code == 200
        prefix = "/v1/runtime/runtime" if gateway is None else "/v1/gateways/gateway/runtimes/runtime"
        assert client.delete(prefix + "/sandbox-scope").status_code == 200
        assert_released(state.predictor)
        assert not state.predictor._context_histories


def test_missing_monitor_sample_still_releases_prediction_state(tmp_path, monkeypatch):
    state = build_state(SidecarConfig(trace_dir=tmp_path))
    with TestClient(create_app(state)) as client:
        req = request()
        client.post("/v1/decisions/tool", json=req.model_dump(mode="json"))
        monkeypatch.setattr(state.tool_monitor, "complete", lambda *args, **kwargs: None)
        event = _tool_completion("done", "call").model_copy(update={"gateway_id": "gateway", "runtime_id": "runtime"})
        assert client.post("/v1/events/tool-completed", json=event.model_dump(mode="json")).status_code == 200
        assert_released(state.predictor)
        assert not state.predictor.duration_context_kb.samples


@pytest.mark.parametrize("runtime", ["runtime", None])
def test_repeated_turns_do_not_count_as_independent_tasks(predictor, runtime, monkeypatch):
    # A coarse or adjusted wall clock must not hide already completed calls.
    monkeypatch.setattr("clawtune_sidecar.predictors.tool_resource.time.time", lambda: 1000.)
    for i in range(6):
        req = request(str(i), "python -m pytest -q", runtime_id=runtime, run_id=f"turn-{i}")
        predictor.record_tool_started(req)
        predictor.predict(req)
        event = _tool_completion("done-" + str(i), str(i)).model_copy(update={
            "gateway_id": req.gateway_id, "runtime_id": runtime, "run_id": req.run_id,
            "succeeded": False, "raw_result": "python: No module named pytest\nExit code: 1"})
        predictor.observe_completion(event, _runtime_sample(str(i), str(i)))
    group = max(predictor.duration_context_kb.groups["failure"].values(), key=len)
    assert len(group) == 5
    assert len(independent_tasks(group)) == 1
    assert predictor.duration_context_kb.predict(group[0]["keys"], 5000) is None


def test_runtime_identity_ignores_session_changes_and_run_only_is_unknown():
    assert task_instance_key(request()) == task_instance_key(request(run_id="retry", session_id="new-session"))
    assert task_instance_key(request(runtime_id=None, session_id=None)) is None


def test_load_merges_legacy_task_identity_preserving_observations(tmp_path):
    kb = DurationContextKB()
    for i in range(6):
        kb.observe({"evidence": "e", "failure": "f"}, ident=str(i),
                   task=str((("gateway", "runtime", "session"), f"run-{i}")),
                   duration_ms=20 + i, failure="missing_module")
    payload = kb.to_json_obj()
    (tmp_path / SNAPSHOT_NAME).write_text(json.dumps(payload))
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=(), ebpf_trace_paths=(), buckets=LatencyBuckets((100, 500)), artifact_dir=tmp_path)
    try:
        restored = predictor.duration_context_kb
        assert len(independent_tasks(restored.samples.values())) == 1
        assert restored.predict({"evidence": "e", "failure": "f"}, 5000) is None
        for old, new in zip(payload["samples"], restored.samples.values()):
            assert {k: v for k, v in old.items() if k != "task"} == {k: v for k, v in new.items() if k != "task"}
    finally:
        predictor.close()
    saved = json.loads((tmp_path / SNAPSHOT_NAME).read_text())
    assert len({row["task"] for row in saved["samples"]}) == 1
    assert DurationContextKB.from_json_obj(saved).to_json_obj() == saved


def test_ambiguous_legacy_runs_do_not_supply_task_support():
    kb = DurationContextKB()
    for i in range(6):
        kb.observe({"evidence": "e", "failure": "f"}, ident=str(i),
                   task=str((("gateway", None, f"run-{i}"), f"run-{i}")), duration_ms=20, failure="missing_module")
    restored = DurationContextKB.from_json_obj(kb.to_json_obj())
    assert len(restored.samples) == 6
    assert not independent_tasks(restored.samples.values())
    assert restored.predict({"evidence": "e", "failure": "f"}, 5000) is None


def test_frozen_rules_are_loaded_once_and_never_generated_or_written(tmp_path, predictor, monkeypatch):
    seed = tmp_path / "seed"
    context_kb = DurationContextKB()
    for i in range(6):
        context_kb.observe({"evidence": "e", "failure": "f"}, ident=str(i),
            task=str((("gateway", "runtime", "session"), f"run-{i}")), duration_ms=20, failure="missing_module")
    payloads = dict(zip(FILES, (predictor.kb.to_json_obj(), predictor.continuous_kb.to_json_obj(),
                              predictor.lattice_kb.to_json_obj())))
    payloads[SNAPSHOT_NAME] = context_kb.to_json_obj()
    create_seed(seed, payloads, provenance={})
    context = ParseContext(repo="repo", runtime_id="runtime", tool_version="1.0")
    rule = generated_rule("known-tool")
    rule.update(names=["known-tool"], canonical="known-tool",
                scope={"kind": "runtime", "repo": "repo", "runtime_id": "runtime", "entry": "known-tool", "tool_version": "1.0"})
    RuleRegistry(seed / "command-rules").add(rule)
    before = {str(p.relative_to(seed)): p.read_bytes() for p in seed.rglob("*") if p.is_file()}
    monkeypatch.setattr(RuleGenerator, "_request", lambda *a: pytest.fail("frozen provider call"))
    state = build_state(SidecarConfig(trace_dir=tmp_path / "trace", tool_resource_artifact_dir=seed,
        tool_resource_frozen=True, llm_proxy_upstream_base_url="https://example.invalid",
        llm_proxy_upstream_api_key="test", llm_proxy_upstream_model="test"))
    try:
        service = state.predictor.command_features
        assert len(independent_tasks(state.predictor.duration_context_kb.samples.values())) == 1
        assert service.generator is None
        assert service.argv(["known-tool", "-n", "4"], context).status == "complete"
        first = service.argv(["unknown-tool", "-n", "4"], context)
        service.discover(context)
        assert service.argv(["unknown-tool", "-n", "4"], context) == first
        with pytest.raises(RuntimeError, match="read-only"):
            service.registry.add(rule, persist=False)
        assert first.status == "partial"
    finally:
        state.predictor.close()
    assert {str(p.relative_to(seed)): p.read_bytes() for p in seed.rglob("*") if p.is_file()} == before


def test_writable_service_generates_and_persists_rule_through_production_wiring(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "task.py").write_text(
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('-n')\np.parse_args()\n",
        encoding="utf-8",
    )
    state = build_state(SidecarConfig(
        trace_dir=tmp_path / "trace",
        tool_resource_artifact_dir=tmp_path / "kb",
        llm_proxy_upstream_base_url="https://example.invalid",
        llm_proxy_upstream_api_key="shared-key",
        llm_proxy_upstream_model="test-model",
    ))
    service = state.predictor.command_features
    service.generator.client = lambda body: copy.deepcopy(generated_rule())
    context = ParseContext(
        repo="repo", runtime_id="runtime", workspace=str(workspace), cwd=str(workspace),
    )
    try:
        before = service.argv(["python", "task.py", "-n", "4"], context)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(
                row["status"] in {"ready", "failed"} for row in service.generator.status().values()):
            time.sleep(.01)
        assert any(row["status"] == "ready" for row in service.generator.status().values()), service.generator.status()
        after = service.argv(["python", "task.py", "-n", "4"], context)
        assert before.status == "partial" and after.status == "complete"
        assert list((tmp_path / "kb" / "command-rules").glob("*.json"))
    finally:
        state.predictor.close()


@pytest.mark.parametrize("version", [None, "1.0"])
def test_generated_rule_binds_known_tool_version(tmp_path, version):
    class Help:
        def acquire(self, argv, context):
            from command_parser.evidence import record
            return record("help", argv[0], "usage: unknown-tool [-n COUNT]", "test")
    context = ParseContext(repo="repo", runtime_id="runtime", tool_version=version)
    registry = RuleRegistry(tmp_path)
    generator = RuleGenerator(registry, GeneratorConfig("https://example.invalid", "test", "test"),
        client=lambda body: generated_rule("unknown-tool"), help_runner=Help())
    try:
        rule = generator.generate("unknown-tool", context)
        assert rule["scope"].get("tool_version") == version
        registry.add(rule)
        reloaded = CommandFeatureService(RuleRegistry(tmp_path))
        assert reloaded.argv(["unknown-tool", "-n", "4"], context).status == "complete"
        if version:
            for changed in (None, "2.0"):
                assert reloaded.argv(["unknown-tool", "-n", "4"], replace(context, tool_version=changed)).status == "partial"
        assert reloaded.argv(["unknown-tool", "-n", "4"], replace(context, runtime_id="other")).status == "partial"
    finally:
        generator.close()
