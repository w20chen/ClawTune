"""Shared SWE execution policy and persistent native-worker admission."""
from types import SimpleNamespace
import os
from pathlib import Path
import threading
import time

import pytest

from benchmarks.exec_control import SidecarExecutions


@pytest.mark.parametrize("override,enabled", [(None, True), ("off", False), ("1", True)])
def test_terminal_requests_swe_host_cgroup_gate(monkeypatch, override, enabled):
    if override is None:
        monkeypatch.delenv("CLAWTUNE_HOST_CGROUP_GATE", raising=False)
    else:
        monkeypatch.setenv("CLAWTUNE_HOST_CGROUP_GATE", override)
    client = SidecarExecutions(1234)
    calls = []
    def request(method, endpoint, payload, **kwargs):
        calls.append(payload)
        return {"stored": True}
    monkeypatch.setattr(client, "_request", request)
    client.started("exec", "token", launcher_pid=1, container_pid=2,
                   namespace_inode=3, starttime_ticks=4, container_id="task")
    assert calls[0]["host_cgroup_gate"] is enabled
    assert calls[0]["cgroup_required"] is False


def test_native_scope_client_is_runtime_owned(monkeypatch):
    client = SidecarExecutions(1234)
    calls = []
    monkeypatch.setattr(client, "_request", lambda *a, **kw: calls.append((a, kw)))
    identity = {"pid": 1, "pid_namespace_inode": 2, "process_starttime_ticks": 3}
    client.provision_native_scope("task/a", identity, gateway_id="bfcl")
    client.delete_native_scope("task/a", gateway_id="bfcl")
    assert calls[0][0] == ("POST", "/v1/gateways/bfcl/runtimes/task%2Fa/native-scope", identity)
    assert calls[1][0][:2] == ("DELETE", "/v1/gateways/bfcl/runtimes/task%2Fa/native-scope")


class StatefulBackend:
    tools, system, turns = [], [], ["task"]

    def __init__(self, task, run_dir):
        self.value = 0

    def call(self, name, arguments, *, call_id=""):
        self.value += arguments["increment"]
        return self.value

    def close(self):
        pass


class MemoryBackend(StatefulBackend):
    def call(self, name, arguments, *, call_id=""):
        if name == "allocate":
            self.buffer = bytearray(32 * 1024 * 1024)
            time.sleep(.3)
        return len(self.buffer)


class InitializationMarker(StatefulBackend):
    def __init__(self, task, run_dir):
        (run_dir / "initialized").touch()
        super().__init__(task, run_dir)


def test_bfcl_keeps_state_across_native_calls(tmp_path):
    from benchmarks.backends import BFCLBackend
    backend = BFCLBackend(SimpleNamespace(), tmp_path, _factory=StatefulBackend)
    try:
        assert backend.call("add", {"increment": 2}) == 2
        assert backend.call("add", {"increment": 3}) == 5
    finally:
        backend.close()


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="Linux worker identity")
def test_bfcl_admission_precedes_state_loading(monkeypatch, tmp_path):
    from benchmarks import backends
    calls = []
    class Sidecar:
        def __init__(self, port):
            pass
        def provision_native_scope(self, runtime, identity, **kwargs):
            assert identity["pid"] > 0 and identity["process_starttime_ticks"] > 0
            calls.append("admit")
        def delete_native_scope(self, *args, **kwargs):
            calls.append("delete")
    monkeypatch.setattr(backends, "SidecarExecutions", Sidecar)
    backend = backends.BFCLBackend(SimpleNamespace(), tmp_path, _factory=StatefulBackend,
                                   sidecar_port=1234, runtime_id="task", gateway_id="bfcl")
    try:
        assert calls == ["admit"]
        assert backend.call("add", {"increment": 7}) == 7
        backend.quiesce()
        assert calls == ["admit"]  # scope survives until finalizers drain
    finally:
        backend.close()
        backend.release_scope()
    assert calls == ["admit", "delete"]


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="Linux worker identity")
def test_bfcl_rejected_admission_never_loads_task_state(monkeypatch, tmp_path):
    from benchmarks import backends
    deleted = []
    class Sidecar:
        def __init__(self, port):
            pass
        def provision_native_scope(self, *args, **kwargs):
            raise RuntimeError("admission refused")
        def delete_native_scope(self, *args, **kwargs):
            deleted.append(True)
    monkeypatch.setattr(backends, "SidecarExecutions", Sidecar)
    with pytest.raises(RuntimeError, match="admission refused"):
        backends.BFCLBackend(SimpleNamespace(), tmp_path, _factory=InitializationMarker,
                            sidecar_port=1234, runtime_id="task", gateway_id="bfcl")
    assert deleted == [True]
    assert not (tmp_path / "initialized").exists()


@pytest.mark.skipif(os.getenv("CLAWTUNE_TEST_NATIVE_CGROUP") != "1",
                   reason="opt-in Linux cgroup delegation test")
def test_live_native_worker_cgroup_and_memory(monkeypatch, tmp_path, record_property):
    """No model or dataset: real kernel charge, gated worker and shared API."""
    from fastapi.testclient import TestClient
    from benchmarks import backends
    from clawtune_sidecar.api.app import create_app
    from clawtune_sidecar.api.dependencies import build_state
    from clawtune_sidecar.config import SidecarConfig
    from clawtune_sidecar.contracts.models import ResourceScope
    from clawtune_sidecar.monitoring.environment_memory import EnvironmentMemoryMonitor

    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    with TestClient(create_app(state)) as client:
        class Sidecar:
            def __init__(self, port):
                pass
            def provision_native_scope(self, runtime, identity, **kwargs):
                response = client.post("/v1/gateways/bfcl/runtimes/real-worker/native-scope", json=identity)
                assert response.status_code == 200, response.text
                self.scope = ResourceScope.model_validate(response.json())
                return response.json()
            def delete_native_scope(self, *args, **kwargs):
                response = client.delete("/v1/gateways/bfcl/runtimes/real-worker/native-scope")
                assert response.status_code == 200, response.text
        monkeypatch.setattr(backends, "SidecarExecutions", Sidecar)
        backend = backends.BFCLBackend(SimpleNamespace(), tmp_path, _factory=MemoryBackend,
            sidecar_port=1234, runtime_id="real-worker", gateway_id="bfcl")
        monitor = EnvironmentMemoryMonitor()
        stopped = threading.Event()
        def poll():
            while not stopped.wait(.02):
                monitor.poll()
        sampler = threading.Thread(target=poll)
        try:
            scope = backend.sidecar.scope
            membership = Path(f"/proc/{backend._worker.pid}/cgroup").read_text().strip().split("::")[-1]
            assert Path(scope.cgroup_path) == Path("/sys/fs/cgroup") / membership.lstrip("/")
            monitor.begin("allocate", scope)
            sampler.start()
            start = time.time()
            assert backend.call("allocate", {}) == 32 * 1024 * 1024
            end = time.time()
            labels = monitor.complete("allocate", started_at=start, ended_at=end)
            assert labels["memory_eligible"], labels
            assert labels["memory_baseline_bytes"] > 0, labels
            assert labels["memory_extra_peak_bytes"] >= 24 * 1024 * 1024, labels
            for field in ("memory_baseline_bytes", "memory_total_peak_bytes", "memory_extra_peak_bytes"):
                record_property(field, labels[field])
            assert backend.call("read", {}) == 32 * 1024 * 1024
        finally:
            stopped.set()
            if sampler.ident is not None:
                sampler.join()
            backend.close()
            backend.release_scope()
        assert not Path(scope.cgroup_path).exists()
