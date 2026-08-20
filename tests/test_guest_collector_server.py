from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "guest_collector_server.py"
    spec = importlib.util.spec_from_file_location("guest_collector_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCollector:
    instances: list["_FakeCollector"] = []

    def __init__(self, **kwargs):
        self.artifact_path = kwargs["artifact_path"]
        self.kwargs = kwargs
        self.finalized: list[str] = []
        self.instances.append(self)

    def begin_tool_call(self, execution_id: str, command: str):
        return (execution_id, command)

    def finish_tool_call(self, token, *, replay_response):
        assert replay_response["returncode"] == 0
        return {"eligible_for_kb": True, "telemetry_quality": "ok"}

    def finalize(self, *, replay_execution: str):
        self.finalized.append(replay_execution)
        self.artifact_path.write_text(
            json.dumps(
                {
                    "collection_validity": "valid",
                    "cleanup": "ok",
                    "telemetry_loss_total": {"total": 0},
                }
            ),
            encoding="utf-8",
        )


def _request(op: str, **values):
    return {"v": 1, "token": "t" * 32, "op": op, **values}


def test_service_begin_finish_uses_exact_explicit_scope(monkeypatch, tmp_path) -> None:
    module = _module()
    monkeypatch.setattr(module, "ClauseTelemetryCollector", _FakeCollector)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    service = module.CollectorService(
        token="t" * 32, artifact_root=tmp_path / "artifacts", max_active=2
    )

    begun = service.dispatch(
        _request(
            "begin",
            execution_id="exec-1",
            command="echo ok",
            cgroup_path=str(cgroup),
            trusted_root_pid=42,
            repo="tenant/repo",
        )
    )
    finished = service.dispatch(
        _request("finish", execution_id="exec-1", return_code=0)
    )

    assert begun["state"] == "observing"
    assert finished == {
        "ok": True,
        "v": 1,
        "execution_id": "exec-1",
        "artifact_path": begun["artifact_path"],
        "eligible_for_kb": True,
        "telemetry_quality": "ok",
        "collection_validity": "valid",
        "cleanup": "ok",
        "loss_total": 0,
    }
    assert _FakeCollector.instances[-1].kwargs["container_id"] is None
    assert _FakeCollector.instances[-1].kwargs["trusted_root_pid"] == 42


def test_service_auth_limit_and_abort_fail_closed(monkeypatch, tmp_path) -> None:
    module = _module()
    monkeypatch.setattr(module, "ClauseTelemetryCollector", _FakeCollector)
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    service = module.CollectorService(
        token="t" * 32, artifact_root=tmp_path / "artifacts", max_active=1
    )
    begin = _request(
        "begin",
        execution_id="exec-1",
        command="true",
        cgroup_path=str(cgroup),
        trusted_root_pid=7,
    )
    service.dispatch(begin)

    with pytest.raises(PermissionError, match="authentication_failed"):
        service.dispatch({**_request("health"), "token": "wrong"})
    with pytest.raises(RuntimeError, match="active_execution_limit_reached"):
        service.dispatch({**begin, "execution_id": "exec-2"})

    response = service.dispatch(_request("abort", execution_id="exec-1"))
    assert response["state"] == "aborted"
    assert _FakeCollector.instances[-1].finalized == ["incomplete"]


def test_prepare_guest_mounts_mounts_tracefs_and_remounts_cgroup(monkeypatch) -> None:
    module = _module()
    calls: list[list[str]] = []

    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(module.Path, "mkdir", lambda self, **kwargs: None)
    monkeypatch.setattr(module.Path, "exists", lambda self: False)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, check: calls.append(argv),
    )

    module._prepare_guest_mounts()

    assert calls == [
        ["mount", "-t", "tracefs", "tracefs", "/sys/kernel/tracing"],
        ["mount", "-t", "tracefs", "tracefs", "/sys/kernel/debug/tracing"],
        ["mount", "-o", "remount,rw", "/sys/fs/cgroup"],
    ]


def test_prepare_guest_mounts_requires_root(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)

    with pytest.raises(RuntimeError, match="must run as root"):
        module._prepare_guest_mounts()
