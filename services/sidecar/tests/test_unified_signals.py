"""Protocol, causality, delivery and real sidecar ingress contracts."""
import asyncio
import time
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError

from clawtune_kb.contracts import validate
from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.contracts.models import ToolPrediction
from clawtune_sidecar.signal_protocol import envelope, event_signal
from clawtune_sidecar.signals import SignalHub
from prediction_fixtures import prediction_example


def prepared(baseline=5000):
    hub = SignalHub()
    owner = {"gateway_id": "g", "runtime_id": "r", "run_id": None, "session_id": None,
             "tool_call_id": "call", "prediction_id": "prediction", "execution_id": "exec"}
    hub.baselines["prediction"] = (owner, baseline)
    hub.register(SimpleNamespace(**owner))
    assert hub.observe(envelope("execution.started", {"pid": 123, "progress_adapter": "PytestProgress"},
                                source="launcher", identity=owner))
    return hub, owner


def progress(owner, elapsed=6200, remaining=1500, **kwargs):
    observed = kwargs.pop("observed_at", time.time())
    return envelope("execution.progress", {
        "elapsed_duration_ms": elapsed, "remaining_duration_ms": remaining,
        "remaining_source": "pytest.completed_percent", "remaining_kind": "estimated",
    }, source="launcher", identity=owner, observed_at=observed,
        expires_at=observed + min(2, remaining / 1000), **kwargs)


def test_progress_update_and_finishing_have_distinct_semantics_and_same_stream():
    hub, owner = prepared()
    assert hub.observe(progress(owner))
    messages = list(hub.recent)
    assert [m["type"] for m in messages] == ["execution.started", "prediction.updated", "execution.finishing_soon"]
    assert [m["sequence"] for m in messages] == [1, 2, 3]
    update = messages[1]
    assert update["payload"]["predicted_total_duration_ms"] == 7700
    assert update["payload"]["baseline_prediction_id"] == "prediction"
    assert update["payload"]["statistic"] == "point_estimate"
    assert len({m["publisher_id"] for m in messages}) == 1
    for m in messages:
        validate(m, "signal.schema.json")
    hub.observe(progress(owner, elapsed=7000))
    assert len(hub.recent) == 3


def test_strict_overrun_boundary_and_finishing_without_any_history():
    hub, owner = prepared(0)
    hub.observe(progress(owner, elapsed=1000, remaining=4000))
    assert len(hub.recent) == 1
    hub.observe(progress(owner, elapsed=1001, remaining=4000))
    assert hub.recent[-1]["type"] == "prediction.updated"
    hub, owner = prepared(None)
    hub.observe(progress(owner, remaining=2000))
    assert [m["type"] for m in hub.recent] == ["execution.started", "execution.finishing_soon"]


def test_stale_out_of_order_and_post_exit_progress_never_reopens_execution():
    hub, owner = prepared()
    assert not hub.observe(progress(owner, observed_at=time.time() - 3))
    assert not hub.observe(progress(owner, observed_at=time.time() + 3))
    hub.observe(progress(owner, elapsed=1000, remaining=4000))
    assert not hub.observe(progress(owner, elapsed=999))
    exit_payload = {"exit_code": 0, "signal": None, "confirmed_timeout": False,
                    "elapsed_duration_ms": 250.0}
    hub.exited("exec", exit_payload, observed_at=time.time())
    assert not hub.observe(progress(owner))
    late_start = envelope("execution.started", {"pid": 456, "progress_adapter": "PytestProgress"},
                          source="launcher", identity=owner)
    assert not hub.observe(late_start)
    hub.exited("exec", exit_payload, observed_at=time.time())
    assert [m["type"] for m in hub.recent] == ["execution.started", "execution.exited"]


def test_completion_fences_progress_without_suppressing_later_process_exit():
    hub = SignalHub()
    owner = {"gateway_id": "g", "runtime_id": "r", "run_id": None, "session_id": None,
             "agent_id": None, "tool_call_id": "call", "call_id": None,
             "prediction_id": "prediction", "execution_id": "exec"}
    hub.register(SimpleNamespace(**owner))
    event = SimpleNamespace(
        **owner,
        decision_id="prediction",
        tool_name="exec",
        duration_ms=1,
        error_type=None,
        occurred_at="2026-01-01T00:00:00+00:00",
    )

    hub.completed(event)
    assert not hub.observe(envelope(
        "execution.started", {"pid": 123, "progress_adapter": "ToolProgress"},
        source="launcher", identity=owner,
    ))
    exit_observed_at = time.time() - .25
    exit_payload = {"exit_code": 0, "signal": None, "confirmed_timeout": False,
                    "elapsed_duration_ms": 250.0}
    hub.exited("exec", exit_payload, observed_at=exit_observed_at)
    hub.exited("exec", exit_payload, observed_at=exit_observed_at)

    assert [m["type"] for m in hub.recent] == ["tool.completed", "execution.exited"]
    assert hub.recent[-1]["observed_at"] == exit_observed_at
    assert hub.recent[-1]["payload"]["elapsed_duration_ms"] == 250.0


def test_receipt_rejects_payload_reuse_and_isolates_runtime():
    hub, owner = prepared()
    message = progress(owner)
    key, digest, duplicate = hub.duplicate(message)
    assert not duplicate
    hub.acknowledge(key, digest)
    assert hub.duplicate(message)[2]
    changed = deepcopy(message)
    changed["payload"]["remaining_duration_ms"] = 900
    with pytest.raises(ValueError, match="mismatch"):
        hub.duplicate(changed)
    changed["identity"]["runtime_id"] = "another"
    assert not hub.duplicate(changed)[2]


def test_prediction_binding_uses_prediction_id_and_checks_owner():
    hub, owner = prepared()
    owner = {**owner, "runtime_id": "foreign", "execution_id": "other"}
    with pytest.raises(ValueError, match="owner_mismatch"):
        hub.register(SimpleNamespace(**owner))
    assert "other" not in hub.executions


@pytest.mark.parametrize("change", [
    lambda m: m["payload"].update(remaining_duration_ms=-1),
    lambda m: m["payload"].update(remaining_kind="confidence"),
    lambda m: m.update(source="gpu"),
    lambda m: m["identity"].update(unscoped="bad"),
])
def test_schema_rejects_invalid_observation(change):
    _, owner = prepared()
    message = progress(owner)
    change(message)
    with pytest.raises(ValidationError):
        validate(message, "signal.schema.json")


@pytest.mark.parametrize("config", [
    {"signal_endpoint": "file:///tmp/events"},
    {"signal_endpoint": "http://user:secret@receiver/events"},
    {"signal_endpoint": "http://receiver:invalid/events"},
    {"signal_queue_size": 0},
    {"signal_timeout_s": float("inf")},
])
def test_signal_configuration_rejects_invalid_transport(config):
    with pytest.raises(ValueError):
        SidecarConfig(**config)


def test_delivery_retries_same_id_and_drops_eta_after_exit():
    async def run():
        received = []
        entered, release = asyncio.Event(), asyncio.Event()
        async def receiver(request):
            import json
            message = json.loads(request.content)
            validate(message, "signal.schema.json")
            received.append(message)
            assert request.headers["authorization"] == "Bearer sink-token"
            if len(received) == 1:
                entered.set()
                await release.wait()
                return httpx.Response(503)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
            hub, owner = prepared()
            hub.endpoint, hub.token, hub.client = "http://receiver/v1/signals", "sink-token", client
            first = hub.publish("execution.started", {"pid": 123, "progress_adapter": "ToolProgress"}, source="launcher", owner=owner)
            await entered.wait()
            hub.observe(progress(owner))
            hub.exited(
                "exec",
                {"exit_code": 0, "signal": None, "confirmed_timeout": False,
                 "elapsed_duration_ms": 250.0},
                observed_at=time.time(),
            )
            release.set()
            await asyncio.wait_for(hub.queue.join(), 2)
            assert [m["type"] for m in received] == ["execution.started", "execution.started", "execution.exited"]
            assert received[0] == received[1] == first
            assert hub.counts["expired"] == 2
            await hub.close()
    asyncio.run(run())


def test_bounded_queue_and_shutdown_do_not_hold_tool_execution():
    async def run():
        entered = asyncio.Event()
        async def receiver(_request):
            entered.set()
            await asyncio.sleep(100)
        async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as client:
            hub = SignalHub("http://receiver/signals", queue_size=1, timeout=100, client=client)
            data = {"pid": 123, "progress_adapter": "ToolProgress"}
            hub.publish("execution.started", data)
            await entered.wait()
            hub.publish("execution.started", data)
            hub.publish("execution.started", data)
            assert hub.counts["queue_full"] == 1
            started = time.monotonic()
            await hub.close()
            assert time.monotonic() - started < 2
            assert hub.task.done()
    asyncio.run(run())


def test_real_api_prediction_ingress_and_completion_use_one_stream(tmp_path, monkeypatch):
    from test_tool_resource_predictor import _runtime_sample, _tool_completion, _tool_request
    state = build_state(SidecarConfig(trace_dir=tmp_path, tool_resource_ebpf_enabled=False, pmu_enabled=False))
    monkeypatch.setattr(state.predictor, "predict", lambda req: prediction_example())
    monkeypatch.setattr(state.tool_monitor, "complete", lambda *a, **k: _runtime_sample("done", "call"))
    app = create_app(state)
    with TestClient(app) as client:
        request = _tool_request("before", "call", "true").model_dump(mode="json")
        decision = client.post("/v1/decisions/tool", json=request)
        assert decision.status_code == 200, decision.text
        assert "placement_advice" not in decision.json()
        validate(decision.json(), "tool-decision.schema.json")
        retry = client.post("/v1/decisions/tool", json=request)
        assert retry.json() == decision.json()
        initial = app.state.signals.recent[-1]
        assert initial["signal_id"] == decision.json()["prediction_signal_id"]
        assert initial["payload"]["prediction"] == decision.json()["prediction"]
        event = _tool_completion("done", "call").model_copy(update={"decision_id": decision.json()["decision_id"]})
        completed = event_signal(event)
        response = client.post("/v1/signals", json=completed)
        assert response.status_code == 200, response.text
        assert client.post("/v1/signals", json=completed).json() == {"stored": False}
        assert [m["type"] for m in app.state.signals.recent] == ["prediction.created", "tool.completed"]
        for m in app.state.signals.recent:
            validate(m, "signal.schema.json")
        assert client.post("/v1/signals", json=initial).status_code == 422
        assert client.post("/v1/events/tool-completed", json=event.model_dump(mode="json")).status_code == 404


def test_ingress_execution_token_identity_and_completion_fence(tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path, tool_resource_ebpf_enabled=False, pmu_enabled=False))
    app = create_app(state)
    with TestClient(app) as client:
        registration = {"execution_id": "exec", "tool_call_id": "call", "gateway_id": "g", "runtime_id": "r",
                        "run_id": None, "session_key_hash": None, "command_digest": "sha256:" + "a"*64,
                        "command": "true", "backend": "managed-wrapper"}
        response = client.post("/v2/executions", json=registration)
        assert response.status_code == 200, response.text
        claim = client.post("/v2/executions/claim", json={"execution_id": "exec",
            "token": response.json()["one_time_token"], "launcher_pid": 123}).json()
        headers = {"Authorization": "Bearer " + claim["update_token"]}
        start = envelope("execution.started", {"pid": 123, "progress_adapter": "ToolProgress"},
                         source="launcher", identity={"execution_id": "exec"})
        assert client.post("/v1/signals", json=start).status_code == 403
        assert client.post("/v1/signals", json=start, headers=headers).json() == {"stored": True}
        foreign = progress({"execution_id": "exec", "runtime_id": "wrong"})
        assert client.post("/v1/signals", json=foreign, headers=headers).status_code == 409
        assert client.post("/v1/signals", json=progress({"execution_id": "exec"}), headers=headers).json() == {"stored": True}
        state.executions.mark_completed("exec")
        assert client.post("/v1/signals", json=progress({"execution_id": "exec"}, elapsed=9000), headers=headers).json() == {"stored": False}


@pytest.mark.skipif(__import__("os").name != "posix", reason="real POSIX managed execution")
def test_real_launcher_sidecar_and_receiver_end_to_end(tmp_path, monkeypatch):
    import json
    import os
    import shlex
    import socket
    import subprocess
    import sys
    import threading
    from datetime import datetime, timezone
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    import uvicorn
    from test_tool_resource_predictor import _tool_completion, _tool_request

    from clawtune_sidecar.contracts.load_prediction import TARGET_UNITS, CallLoadPrediction
    from clawtune_sidecar.predictors.call_load import summarize

    received = []
    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/v1/signals"
            assert self.headers["Authorization"] == "Bearer receiver-secret"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            validate(data, "signal.schema.json")
            received.append(data)
            self.send_response(200)
            self.end_headers()

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces", auth_token="sidecar-secret",
        tool_resource_ebpf_enabled=False, pmu_enabled=False,
        signal_endpoint=f"http://127.0.0.1:{receiver.server_port}/v1/signals", signal_token="receiver-secret"))
    lattice = CallLoadPrediction(targets={target: summarize(target, (1, 100), "lattice",
        (0.0,) if target == "duration_ms" else (), reason="test_unavailable") for target in TARGET_UNITS})
    prediction = prediction_example()
    prediction.lattice.load = lattice
    prediction.tool.load.targets["duration_ms"] = summarize("duration_ms", (100, 500), "runtime", (0, 100))
    monkeypatch.setattr(state.predictor, "predict", lambda request: prediction)
    # Exercise the real execution gate and HTTP protocol, without requiring
    # privileged kernel collector installation in the test environment.
    monkeypatch.setattr(state.predictor, "begin_execution", lambda **kwargs: False)
    app = create_app(state)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    server_thread.start()
    process = None
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        workload = tmp_path / "test_progress_workload.py"
        workload.write_text("import time,pytest,os\n@pytest.mark.parametrize('case', range(8))\n"
            "def test_work(case):\n"
            "    assert 'CLAWTUNE_SIGNAL_UPDATE_TOKEN' not in os.environ\n"
            "    assert 'CLAWTUNE_TOKEN' not in os.environ\n"
            "    assert 'CLAWTUNE_SIGNAL_TOKEN' not in os.environ\n"
            "    time.sleep(.4)\n")
        command = shlex.join([sys.executable, "-m", "pytest", "-v", "--color=no", str(workload)])
        request = _tool_request("before", "call", command).model_copy(update={"runtime_id": "runtime", "gateway_id": "gateway"})
        with httpx.Client(base_url=endpoint, headers={"Authorization": "Bearer sidecar-secret"}, timeout=15, trust_env=False) as client:
            decision = client.post("/v1/decisions/tool", json=request.model_dump(mode="json"))
            assert decision.status_code == 200, decision.text
            decision = decision.json()
            spec = {"execution_id": "exec", "prediction_id": decision["decision_id"],
                    "command": command, "command_digest": request.params_digest,
                    "session_key_hash": None, "backend": "managed-wrapper",
                    "profiling": {"enable_cgroup": False, "enable_affinity": False, "enable_numa": False},
                    **{k: getattr(request, k) for k in ("run_id", "session_id", "agent_id", "tool_call_id", "gateway_id", "runtime_id")}}
            registered = client.post("/v2/executions", json=spec)
            assert registered.status_code == 200, registered.text
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                CLAWTUNE_EXECUTION_TOKEN=registered.json()["one_time_token"], CLAWTUNE_TOKEN="sidecar-secret",
                CLAWTUNE_SIGNAL_TOKEN="receiver-secret", CLAWTUNE_TELEMETRY_FAIL_OPEN="1",
                CLAWTUNE_HOST_CGROUP_GATE="0", CLAWTUNE_CGROUP_REQUIRED="0", CLAWTUNE_LAUNCH_MODE="subprocess")
            process = subprocess.Popen([sys.executable, "-m", "clawtune_sidecar.launcher", "run",
                "--execution-id", "exec", "--endpoint", endpoint], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate(timeout=25)
            assert process.returncode == 0, stderr.decode()
            assert b"8 passed" in stdout
            event = _tool_completion("done", "call").model_copy(update={
                "execution_id": "exec", "decision_id": decision["decision_id"],
                "gateway_id": "gateway", "runtime_id": "runtime",
                "occurred_at": datetime.now(timezone.utc).isoformat()})  # noqa: UP017 - Python 3.10
            result = client.post("/v1/signals", json=event_signal(event))
            assert result.status_code == 200, result.text
            deadline = time.monotonic() + 5
            while (not received or received[-1]["type"] != "tool.completed") and time.monotonic() < deadline:
                time.sleep(.02)
            kinds = [m["type"] for m in received]
            assert kinds == ["prediction.created", "execution.started", "prediction.updated",
                             "execution.finishing_soon", "execution.exited", "tool.completed"]
            assert received[0]["signal_id"] == decision["prediction_signal_id"]
            assert all(m["identity"]["prediction_id"] == decision["decision_id"] for m in received)
            assert all(m["identity"]["runtime_id"] == "runtime" for m in received)
            assert all(m["identity"]["execution_id"] == "exec" for m in received[1:])
            assert [m["sequence"] for m in received] == list(range(1, 7))
            exited = received[kinds.index("execution.exited")]
            assert exited["observed_at"] <= exited["published_at"]
            assert exited["payload"]["elapsed_duration_ms"] > 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.should_exit = True
        server_thread.join(timeout=15)
        listener.close()
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=2)
    assert not server_thread.is_alive()
