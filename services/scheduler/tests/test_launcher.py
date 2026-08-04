from __future__ import annotations

import os
import shlex
import time
from typing import Any

import pytest

from agent_scheduler import launcher


class _FakeChild:
    pid = 4242

    def wait(self) -> int:
        return 7


def test_launcher_selects_explicit_fork_exec_mode(monkeypatch) -> None:
    monkeypatch.setenv("CLAW_LAUNCH_MODE", "fork-exec")
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher.os, "fork", lambda: 123, raising=False)
    monkeypatch.setattr(launcher, "_run_forkexec", lambda *_args: 19)
    monkeypatch.setattr(
        launcher,
        "_run_subprocess",
        lambda *_args: pytest.fail("subprocess launcher must not be selected"),
    )

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 19


def test_launcher_rejects_fork_exec_mode_without_posix_fork(monkeypatch) -> None:
    monkeypatch.setenv("CLAW_LAUNCH_MODE", "fork-exec")
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: False)

    with pytest.raises(RuntimeError, match="requires POSIX os.fork"):
        launcher.run_execution("http://sidecar", "exec-1", "token-1")


def test_launcher_rejects_unknown_mode(monkeypatch) -> None:
    monkeypatch.setenv("CLAW_LAUNCH_MODE", "mystery")

    with pytest.raises(ValueError, match="unsupported CLAW_LAUNCH_MODE"):
        launcher.run_execution("http://sidecar", "exec-1", "token-1")


def test_launcher_diagnostics_confirms_fork_exec_support(monkeypatch) -> None:
    monkeypatch.setenv("CLAW_LAUNCH_MODE", "fork-exec")
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher.os, "fork", lambda: 123, raising=False)
    monkeypatch.setenv("PATH", "/task/bin:/usr/bin")
    monkeypatch.setattr(
        launcher,
        "which",
        lambda name, *, path=None: f"/resolved/{name}" if path else None,
    )

    assert launcher.launcher_diagnostics() == {
        "mode": "fork-exec",
        "fork_supported": True,
        "ready": True,
        "payload_path": "/task/bin:/usr/bin",
        "payload_python3": "/resolved/python3",
        "payload_pip": "/resolved/pip",
        "payload_pip3": "/resolved/pip3",
    }


def test_launcher_diagnose_command_reports_selected_mode(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        launcher,
        "launcher_diagnostics",
        lambda: {"mode": "fork-exec", "fork_supported": True, "ready": True},
    )
    monkeypatch.setattr("sys.argv", ["claw-launch", "diagnose"])

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 0
    assert '"mode": "fork-exec"' in capsys.readouterr().out


def test_launcher_retries_exit_report_with_bounded_cold_start_timeout(
    monkeypatch,
    capsys,
) -> None:
    attempts: list[tuple[str, float, dict[str, Any]]] = []
    sleeps: list[float] = []
    update_token = "private-update-token"

    def fake_post(
        _endpoint: str,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        attempts.append((path, timeout_seconds, payload))
        if len(attempts) < 2:
            raise TimeoutError("sidecar busy")
        return {"stored": True}

    monkeypatch.setattr(launcher, "_post_json_with_timeout", fake_post)
    monkeypatch.setattr(launcher.time, "sleep", sleeps.append)

    result = launcher._post_json_best_effort(
        "http://sidecar",
        "/v2/executions/exec-1/exited",
        {"update_token": update_token, "exit_code": 0, "signal": None},
    )

    assert result == {"stored": True}
    assert [timeout for _path, timeout, _payload in attempts] == list(
        launcher._SUBPROCESS_EXIT_REPORT_TIMEOUTS_SECONDS
    )
    assert sleeps == [
        launcher._EXIT_REPORT_RETRY_DELAY_SECONDS,
    ]
    captured = capsys.readouterr()
    assert update_token not in captured.out
    assert update_token not in captured.err


def test_launcher_exhausts_bounded_exit_report_timeouts_without_raising(
    monkeypatch,
    capsys,
) -> None:
    attempts: list[tuple[str, float, dict[str, Any]]] = []
    sleeps: list[float] = []
    update_token = "private-update-token"

    def fake_post(
        _endpoint: str,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        attempts.append((path, timeout_seconds, payload))
        raise TimeoutError("sidecar still busy")

    monkeypatch.setattr(launcher, "_post_json_with_timeout", fake_post)
    monkeypatch.setattr(launcher.time, "sleep", sleeps.append)

    result = launcher._post_json_best_effort(
        "http://sidecar",
        "/v2/executions/exec-1/exited",
        {"update_token": update_token, "exit_code": 0, "signal": None},
    )

    assert result == {}
    assert [timeout for _path, timeout, _payload in attempts] == list(
        launcher._SUBPROCESS_EXIT_REPORT_TIMEOUTS_SECONDS
    )
    assert sleeps == [
        launcher._EXIT_REPORT_RETRY_DELAY_SECONDS,
    ]
    captured = capsys.readouterr()
    assert update_token not in captured.out
    assert update_token not in captured.err


def test_fork_exec_exit_report_keeps_original_short_retry_budget(
    monkeypatch,
) -> None:
    attempts: list[float] = []
    sleeps: list[float] = []
    monkeypatch.setenv("CLAW_LAUNCH_MODE", "fork-exec")

    def fake_post(
        _endpoint: str,
        _path: str,
        _payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        attempts.append(timeout_seconds)
        raise TimeoutError("sidecar unavailable")

    monkeypatch.setattr(launcher, "_post_json_with_timeout", fake_post)
    monkeypatch.setattr(launcher.time, "sleep", sleeps.append)

    assert (
        launcher._post_json_best_effort(
            "http://sidecar",
            "/v2/executions/exec-1/exited",
            {"update_token": "private", "exit_code": 0, "signal": None},
        )
        == {}
    )
    assert attempts == list(launcher._FORK_EXEC_EXIT_REPORT_TIMEOUTS_SECONDS)
    assert sleeps == [launcher._EXIT_REPORT_RETRY_DELAY_SECONDS] * 2


def test_fork_exec_registers_before_releasing_child_and_closes_gate(
    monkeypatch,
) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []
    gate_fds: list[int] = []
    releases: list[bytes] = []
    real_pipe = os.pipe

    def tracked_pipe() -> tuple[int, int]:
        read_fd, write_fd = real_pipe()
        gate_fds.extend((read_fd, write_fd))
        return read_fd, write_fd

    def fake_post(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
        if path.endswith("/started"):
            return {"stored": True}
        return {
            "command": "echo hello",
            "workdir": None,
            "update_token": "update-1",
        }

    monkeypatch.setattr(launcher.os, "pipe", tracked_pipe)
    monkeypatch.setattr(launcher.os, "fork", lambda: 4242, raising=False)
    monkeypatch.setattr(
        launcher.os,
        "write",
        lambda _fd, data: releases.append(data) or len(data),
    )
    monkeypatch.setattr(launcher.os, "waitpid", lambda _pid, _flags: (4242, 0))
    monkeypatch.setattr(
        launcher.os, "WIFEXITED", lambda _status: True, raising=False
    )
    monkeypatch.setattr(
        launcher.os, "WEXITSTATUS", lambda _status: 0, raising=False
    )
    monkeypatch.setattr(
        launcher.os, "WIFSIGNALED", lambda _status: False, raising=False
    )
    monkeypatch.setattr(launcher, "_post_json", fake_post)
    monkeypatch.setattr(launcher, "_post_json_best_effort", lambda *_args: {})
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)
    monkeypatch.setattr(
        launcher,
        "_install_fork_signal_forwarders",
        lambda _pid: lambda: None,
    )

    assert launcher._run_forkexec("http://sidecar", "exec-1", "token-1") == 0
    assert [path for path, _payload in posts] == [
        "/v2/executions/claim",
        "/v2/executions/exec-1/started",
    ]
    assert releases == [b"1"]
    for fd in gate_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_fork_exec_reaps_child_without_release_when_started_fails(
    monkeypatch,
) -> None:
    writes: list[bytes] = []
    waits: list[int] = []

    def fake_post(_endpoint: str, path: str, _payload: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/started"):
            raise RuntimeError("collector unavailable")
        return {
            "command": "echo must-not-run",
            "workdir": None,
            "update_token": "update-1",
        }

    monkeypatch.setattr(launcher.os, "fork", lambda: 4242, raising=False)
    monkeypatch.setattr(
        launcher.os,
        "write",
        lambda _fd, data: writes.append(data) or len(data),
    )
    monkeypatch.setattr(
        launcher.os,
        "waitpid",
        lambda pid, _flags: waits.append(pid) or (pid, 126 << 8),
    )
    monkeypatch.setattr(launcher, "_post_json", fake_post)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)

    with pytest.raises(RuntimeError, match="collector unavailable"):
        launcher._run_forkexec("http://sidecar", "exec-1", "token-1")

    assert writes == []
    assert waits == [4242]


def test_exec_gate_requires_explicit_success_byte() -> None:
    for payload, expected in ((b"1", True), (b"", False), (b"0", False)):
        read_fd, write_fd = os.pipe()
        if payload:
            os.write(write_fd, payload)
        os.close(write_fd)
        assert launcher._exec_gate_opened(read_fd) is expected
        with pytest.raises(OSError):
            os.fstat(read_fd)


def test_started_report_allows_bounded_ebpf_cold_start(monkeypatch) -> None:
    attempts: list[tuple[str, float]] = []

    def fake_post(
        _endpoint: str,
        path: str,
        _payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        attempts.append((path, timeout_seconds))
        return {"stored": True}

    monkeypatch.setattr(launcher, "_post_json_with_timeout", fake_post)

    assert launcher._post_json(
        "http://sidecar",
        "/v2/executions/exec-1/started",
        {"update_token": "private"},
    ) == {"stored": True}
    assert attempts == [
        (
            "/v2/executions/exec-1/started",
            launcher._START_REPORT_TIMEOUT_SECONDS,
        )
    ]


def test_launcher_claims_starts_and_returns_child_exit_code(monkeypatch) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
        if path.endswith("/started"):
            return {"stored": True}
        assert path == "/v2/executions/claim"
        return {
            "execution_id": "exec-1",
            "update_token": "update-1",
            "command": "echo hello",
            "command_digest": "sha256:" + "a" * 64,
            "workdir": None,
            "host": "gateway",
            "placement": None,
            "profiling": None,
        }

    def fake_best_effort(_endpoint: str, path: str, payload: dict[str, Any]) -> None:
        posts.append((path, payload))

    def fake_spawn(
        command: str,
        cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ) -> _FakeChild:
        assert command == "echo hello"
        assert cwd is None
        assert cgroup_path is None
        assert affinity_cpus is None
        return _FakeChild()

    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: False)
    monkeypatch.setattr(launcher, "_spawn_shell", fake_spawn)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 7
    assert posts[0] == (
        "/v2/executions/claim",
        {"execution_id": "exec-1", "token": "token-1", "launcher_pid": posts[0][1]["launcher_pid"]},
    )
    assert posts[1] == (
        "/v2/executions/exec-1/started",
        {
            "update_token": "update-1",
            "launcher_pid": posts[1][1]["launcher_pid"],
            "child_pid": 4242,
            "process_starttime_ticks": 99,
            "cgroup_path": None,
            "pid_namespace_inode": 123,
            "container_id": None,
            "host_cgroup_gate": False,
        },
    )
    assert posts[2] == (
        "/v2/executions/exec-1/exited",
        {"update_token": "update-1", "exit_code": 7, "signal": None},
    )


def test_launcher_reports_container_id_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CLAW_SANDBOX_CONTAINER_ID", "a" * 64)
    monkeypatch.delenv("AGENT_SCHEDULER_SANDBOX_CONTAINER_ID", raising=False)

    assert launcher._detect_container_id() == "a" * 64


def test_launcher_reports_container_id_from_self_cgroup(monkeypatch) -> None:
    monkeypatch.delenv("CLAW_SANDBOX_CONTAINER_ID", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_SANDBOX_CONTAINER_ID", raising=False)
    container_id = "b" * 64

    def fake_read_text(self, *args, **kwargs):
        assert str(self).replace("\\", "/") == "/proc/self/cgroup"
        return f"0::/system.slice/docker-{container_id}.scope\n"

    monkeypatch.setattr(launcher.Path, "read_text", fake_read_text)

    assert launcher._detect_container_id() == container_id


def test_launcher_uses_non_login_shell_for_payload(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> _FakeChild:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeChild()

    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    launcher._spawn_shell("printf hello", "/workspace")

    assert captured["args"] == ["/bin/sh", "-c", "printf hello"]
    assert "-l" not in captured["args"]


def test_launcher_reports_started_once_after_spawning_cgroup_payload(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    posts: list[tuple[str, dict[str, Any]]] = []
    released = False

    class GatedChild(_FakeChild):
        def wait(self) -> int:
            assert released is True
            events.append("wait")
            return 7

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        events.append(path)
        posts.append((path, payload))
        if path.endswith("/started"):
            return {"stored": True}
        return {
            "execution_id": "exec-1",
            "update_token": "update-1",
            "command": "echo hello",
            "command_digest": "sha256:" + "a" * 64,
            "workdir": None,
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
        }

    def fake_best_effort(_endpoint: str, path: str, payload: dict[str, Any]) -> None:
        events.append(path)
        posts.append((path, payload))

    def fake_gated_spawn(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ):
        events.append("spawn")
        assert cgroup_path == str(tmp_path / "exec-1")
        assert affinity_cpus is None

        def release(allow: bool = True) -> None:
            nonlocal released
            released = allow
            events.append("release")

        return GatedChild(), release

    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_cgroup_procs_writable", lambda _path: True)
    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_spawn_shell_gated", fake_gated_spawn)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 7
    assert events[:5] == [
        "/v2/executions/claim",
        "spawn",
        "/v2/executions/exec-1/started",
        "release",
        "wait",
    ]
    assert posts[1][1]["child_pid"] == 4242
    assert posts[1][1]["cgroup_path"] == str(tmp_path / "exec-1")


def test_launcher_uses_host_cgroup_gate_for_remote_sidecar(monkeypatch) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []
    released = False

    class GatedChild:
        pid = 5150

        def wait(self) -> int:
            assert released is True
            return 0

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
        if path.endswith("/started"):
            return {
                "stored": True,
                "cgroup_path": "/sys/fs/cgroup/sandbox/claw-executions/exec-1",
            }
        return {
            "execution_id": "exec-1",
            "update_token": "update-1",
            "command": "echo hello",
            "command_digest": "sha256:" + "a" * 64,
            "workdir": None,
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
        }

    def fake_best_effort(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
        if path.endswith("/started"):
            return {"stored": True, "cgroup_path": "/sys/fs/cgroup/sandbox/claw-executions/exec-1"}
        return {"stored": True}

    def fake_gated(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ):
        assert cgroup_path is None
        assert affinity_cpus is None

        def release(allow: bool = True) -> None:
            nonlocal released
            released = allow

        return GatedChild(), release

    monkeypatch.setenv("CLAW_SCHEDULER_ENDPOINT", "http://host.docker.internal:8765")
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher, "_prepare_cgroup", lambda *_args: None)
    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_spawn_shell_gated", fake_gated)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)

    assert launcher.run_execution("http://host.docker.internal:8765", "exec-1", "token-1") == 0
    assert released is True
    assert posts[1][1]["child_pid"] == 5150
    assert posts[1][1]["cgroup_path"] is None
    assert posts[1][1]["host_cgroup_gate"] is True


def test_gated_shell_execs_wrapper_before_waiting_for_release(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    writes: list[tuple[int, bytes]] = []
    closes: list[int] = []

    class GatedChild:
        pid = 6161

    def fake_popen(args: list[str], **kwargs: Any) -> GatedChild:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return GatedChild()

    sentinel_preexec = object()
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher, "_child_preexec", lambda *_args: sentinel_preexec)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher.os, "pipe", lambda: (71, 72))
    monkeypatch.setattr(launcher.os, "write", lambda fd, data: writes.append((fd, data)))
    monkeypatch.setattr(launcher.os, "close", closes.append)

    _child, release = launcher._spawn_shell_gated(
        "printf hello",
        "/workspace",
        cgroup_path="/sys/fs/cgroup/claw/exec-1",
        affinity_cpus={2},
    )

    assert captured["args"][:2] == ["/bin/sh", "-c"]
    wrapper = captured["args"][2]
    assert wrapper.startswith(
        "IFS= read -r _claw_release < /proc/self/fd/71 || exit 125;"
    )
    assert '"$_claw_release" = "claw-release-v1"' in wrapper
    assert 'exec /bin/sh -c "$_claw_payload"' in wrapper
    assert captured["kwargs"]["pass_fds"] == (71,)
    assert captured["kwargs"]["preexec_fn"] is sentinel_preexec
    assert captured["kwargs"]["env"]["CLAW_GATED_PAYLOAD"] == "printf hello"

    release()
    release()
    assert writes == [(72, b"claw-release-v1\n")]
    assert closes == [71, 72]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX subprocess controls")
def test_gated_shell_cannot_exec_payload_before_release(tmp_path) -> None:
    marker = tmp_path / "payload-ran"
    child, release = launcher._spawn_shell_gated(
        f"printf gated > {shlex.quote(str(marker))}",
        str(tmp_path),
    )
    try:
        time.sleep(0.05)
        assert marker.exists() is False
        release()
        assert child.wait(timeout=5) == 0
        assert marker.read_text(encoding="utf-8") == "gated"
    finally:
        release()
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX subprocess controls")
def test_gated_shell_eof_aborts_payload(tmp_path) -> None:
    marker = tmp_path / "payload-must-not-run"
    child, release = launcher._spawn_shell_gated(
        f"printf unsafe > {shlex.quote(str(marker))}",
        str(tmp_path),
    )

    release(False)

    assert child.wait(timeout=5) == 125
    assert marker.exists() is False


def test_launcher_extracts_cpu_and_numa_placement() -> None:
    placement = {"cpu_set": "0-2,4", "numa_node": 1}

    assert launcher._extract_cpu_set(placement) == "0-2,4"
    assert launcher._extract_mems(placement) == "1"
    assert launcher._parse_cpu_list("0-2,4") == {0, 1, 2, 4}


def test_launcher_prepares_cgroup_with_cpuset_order(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_cgroup_procs_writable", lambda _path: True)

    cgroup_path = launcher._prepare_cgroup(
        "exec:1",
        "2-3",
        "0",
        {"enable_cgroup": True},
    )

    assert cgroup_path == str(tmp_path / "exec_1")
    assert (tmp_path / "exec_1" / "cpuset.mems").read_text(encoding="utf-8") == "0"
    assert (tmp_path / "exec_1" / "cpuset.cpus").read_text(encoding="utf-8") == "2-3"


def test_launcher_can_require_cgroup(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAW_CGROUP_REQUIRED", "1")
    monkeypatch.delenv("CLAW_CGROUP_PATH", raising=False)
    monkeypatch.setattr(launcher, "_write_file", lambda _path, _value: (_ for _ in ()).throw(OSError("blocked")))

    with pytest.raises(RuntimeError, match="cgroup_unavailable"):
        launcher._prepare_cgroup(
            "exec:1",
            "0",
            None,
            {"enable_cgroup": True},
        )


def test_launcher_required_cgroup_overrides_profiling_disable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_cgroup_procs_writable", lambda _path: True)
    monkeypatch.setenv("CLAW_CGROUP_REQUIRED", "1")
    monkeypatch.delenv("CLAW_CGROUP_PATH", raising=False)

    cgroup_path = launcher._prepare_cgroup(
        "exec:1",
        None,
        None,
        {"enable_cgroup": False},
    )

    assert cgroup_path == str(tmp_path / "exec_1")


def test_launcher_uses_cgroup_root_when_cgroupfs_is_writable(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher, "_try_candidate_parent", lambda path: path == "/sys/fs/cgroup")
    monkeypatch.setattr(launcher, "_start_user_manager", lambda: None)

    assert launcher._cgroup_root_candidates()[0] == "/sys/fs/cgroup/claw"


def test_launcher_required_cgroup_fails_without_posix(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: False)
    monkeypatch.setenv("CLAW_CGROUP_REQUIRED", "1")

    with pytest.raises(RuntimeError, match="posix_controls_unsupported"):
        launcher._prepare_cgroup(
            "exec:1",
            None,
            None,
            {"enable_cgroup": True},
        )


def test_launcher_required_cgroup_verifies_child_membership(monkeypatch, tmp_path) -> None:
    cgroup_path = tmp_path / "exec-1"
    cgroup_path.mkdir()
    (cgroup_path / "cgroup.procs").write_text("123\n", encoding="utf-8")
    monkeypatch.setenv("CLAW_CGROUP_REQUIRED", "1")

    with pytest.raises(RuntimeError, match="cgroup_join_missing"):
        launcher._verify_child_cgroup(456, str(cgroup_path))

    (cgroup_path / "cgroup.procs").write_text("456\n", encoding="utf-8")
    launcher._verify_child_cgroup(456, str(cgroup_path))


def test_launcher_required_cgroup_reports_parent_join_failure(monkeypatch, tmp_path) -> None:
    cgroup_path = tmp_path / "exec-1"
    cgroup_path.mkdir()
    (cgroup_path / "cgroup.type").write_text("domain", encoding="utf-8")
    (tmp_path / "cgroup.type").write_text("domain threaded", encoding="utf-8")
    monkeypatch.setenv("CLAW_CGROUP_REQUIRED", "1")
    monkeypatch.setattr(launcher, "_write_file", lambda _path, _value: (_ for _ in ()).throw(OSError("blocked")))

    with pytest.raises(RuntimeError, match=r"cgroup_join_failed.*type='domain'.*parent_type='domain threaded'"):
        launcher._join_child_cgroup(456, str(cgroup_path))


def test_launcher_cgroup_join_failure_falls_back_when_not_required(monkeypatch, tmp_path) -> None:
    cgroup_path = tmp_path / "exec-1"
    cgroup_path.mkdir()
    monkeypatch.delenv("CLAW_CGROUP_REQUIRED", raising=False)
    monkeypatch.setattr(launcher, "_write_file", lambda _path, _value: (_ for _ in ()).throw(OSError("blocked")))

    assert launcher._join_child_cgroup(456, str(cgroup_path)) is False


def test_launcher_skips_systemd_scope_without_user_manager(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(
        launcher,
        "which",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )
    monkeypatch.setattr(launcher, "_systemd_user_manager_available", lambda: False)
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "systemd-run must not start without a user manager"
        ),
    )

    assert launcher._restart_in_systemd_scope(
        _FakeChild(),
        "echo hello",
        None,
        execution_id="exec-1",
        affinity_cpus=None,
        profiling={"enable_cgroup": True},
    ) is None


def test_launcher_failed_systemd_fallback_drops_unjoined_cgroup(
    monkeypatch,
    tmp_path,
) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []
    cleaned: list[str] = []
    released: list[bool] = []
    host_gate_checks: list[bool] = []
    original_cgroup = tmp_path / "exec-1"
    original_cgroup.mkdir()

    def fake_post_json(
        _endpoint: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        posts.append((path, payload))
        if path.endswith("/started"):
            return {"stored": True}
        return {
            "execution_id": "exec-1",
            "update_token": "update-1",
            "command": "echo hello",
            "command_digest": "sha256:" + "a" * 64,
            "workdir": None,
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
        }

    def fake_best_effort(
        _endpoint: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        posts.append((path, payload))
        return {}

    def fake_gated_spawn(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ) -> tuple[_FakeChild, Any]:
        assert cgroup_path == str(original_cgroup)
        assert affinity_cpus is None
        return _FakeChild(), lambda allow=True: released.append(allow)

    def fake_host_gate(_profiling: object) -> bool:
        host_gate_checks.append(True)
        return True

    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_prepare_cgroup", lambda *_args: str(original_cgroup))
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher, "_spawn_shell_gated", fake_gated_spawn)
    monkeypatch.setattr(launcher, "_join_child_cgroup", lambda _pid, _path: False)
    monkeypatch.setattr(launcher, "_restart_in_systemd_scope", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_host_cgroup_gate_enabled", fake_host_gate)
    monkeypatch.setattr(launcher, "_cleanup_cgroup", cleaned.append)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)
    monkeypatch.setattr(launcher, "_detect_container_id", lambda: None)

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 7
    started = next(payload for path, payload in posts if path.endswith("/started"))
    assert started["cgroup_path"] is None
    assert started["host_cgroup_gate"] is True
    assert host_gate_checks == [True]
    assert cleaned == [str(original_cgroup)]
    assert released == [True]


def test_launcher_terminates_gated_payload_when_started_is_rejected(
    monkeypatch,
) -> None:
    released: list[bool] = []

    class GatedChild:
        pid = 4242
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            assert self.terminated
            return -15

    child = GatedChild()

    def fake_post_json(
        _endpoint: str,
        path: str,
        _payload: dict[str, Any],
    ) -> dict[str, Any]:
        if path == "/v2/executions/claim":
            return {
                "execution_id": "exec-1",
                "update_token": "update-1",
                "command": "echo must-not-run",
                "command_digest": "sha256:" + "a" * 64,
                "workdir": None,
                "host": "gateway",
                "placement": None,
                "profiling": {"mode": "off"},
            }
        raise RuntimeError("tool_resource_stage2_start_failed")

    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_prepare_cgroup", lambda *_args: None)
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(
        launcher,
        "_spawn_shell_gated",
        lambda *_args, **_kwargs: (
            child,
            lambda allow=True: released.append(allow),
        ),
    )
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)
    monkeypatch.setattr(launcher, "_detect_container_id", lambda: None)

    with pytest.raises(RuntimeError, match="tool_resource_stage2_start_failed"):
        launcher.run_execution("http://sidecar", "exec-1", "token-1")

    assert child.terminated is True
    assert released == [False]


def test_launcher_join_failure_restarts_in_systemd_scope(monkeypatch, tmp_path) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []
    original_cgroup = tmp_path / "exec-1"
    original_cgroup.mkdir()
    systemd_cgroup = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/claw-exec-1.scope"

    class FakeSystemdChild:
        pid = 5151

        def wait(self) -> int:
            return 0

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
        if path.endswith("/started"):
            return {"stored": True}
        return {
            "execution_id": "exec-1",
            "update_token": "update-1",
            "command": "echo hello",
            "command_digest": "sha256:" + "a" * 64,
            "workdir": None,
            "host": "gateway",
            "placement": None,
            "profiling": {"enable_cgroup": True},
        }

    def fake_best_effort(_endpoint: str, path: str, payload: dict[str, Any]) -> None:
        posts.append((path, payload))

    def fake_gated_spawn(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ):
        assert cgroup_path == str(original_cgroup)
        assert affinity_cpus is None
        return _FakeChild(), lambda _allow=True: None

    def fake_popen(args: list[str], **_kwargs: Any) -> FakeSystemdChild:
        assert args[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
        assert "--unit=claw-exec-1.scope" in args
        assert "Delegate=yes" in args
        assert args[-3:-1] == ["/bin/sh", "-c"]
        assert 'exec /bin/sh -c "$CLAW_SYSTEMD_PAYLOAD"' in args[-1]
        assert "-lc" not in args
        return FakeSystemdChild()

    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_cgroup_procs_writable", lambda _path: True)
    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_spawn_shell_gated", fake_gated_spawn)
    monkeypatch.setattr(launcher, "_join_child_cgroup", lambda _pid, _path: False)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)
    monkeypatch.setattr(launcher, "which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(launcher, "_systemd_user_manager_available", lambda: True)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "_read_cgroup_probe", lambda _path: systemd_cgroup)
    monkeypatch.setattr(launcher, "_systemd_unit_cgroup_path", lambda _unit: systemd_cgroup)

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 0
    assert posts[1] == (
        "/v2/executions/exec-1/started",
        {
            "update_token": "update-1",
                "launcher_pid": posts[1][1]["launcher_pid"],
            "child_pid": 5151,
            "process_starttime_ticks": 99,
            "cgroup_path": systemd_cgroup,
            "pid_namespace_inode": 123,
            "container_id": None,
            "host_cgroup_gate": False,
        },
    )


def test_launcher_passes_placement_to_spawn(monkeypatch, tmp_path) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
        if path.endswith("/started"):
            return {"stored": True}
        return {
            "execution_id": "exec-1",
            "update_token": "update-1",
            "command": "echo hello",
            "command_digest": "sha256:" + "a" * 64,
            "workdir": None,
            "host": "gateway",
            "placement": {"cpu_set": "1,3", "numa_node": 0},
            "profiling": {"enable_cgroup": True, "enable_affinity": True},
        }

    def fake_best_effort(_endpoint: str, path: str, payload: dict[str, Any]) -> None:
        posts.append((path, payload))

    def fake_gated_spawn(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ):
        assert cgroup_path == str(tmp_path / "exec-1")
        assert affinity_cpus == {1, 3}
        return _FakeChild(), lambda _allow=True: None

    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_cgroup_procs_writable", lambda _path: True)
    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_spawn_shell_gated", fake_gated_spawn)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 7
    assert posts[1][1]["cgroup_path"] == str(tmp_path / "exec-1")


def test_launcher_accepts_dash_prefixed_token_with_equals(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def fake_run(endpoint: str, execution_id: str, token: str) -> int:
        seen["endpoint"] = endpoint
        seen["execution_id"] = execution_id
        seen["token"] = token
        return 0

    monkeypatch.setattr(launcher, "run_execution", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "claw-launch",
            "run",
            "--endpoint",
            "http://sidecar",
            "--execution-id",
            "exec-1",
            "--token=-leading-token",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 0
    assert seen == {
        "endpoint": "http://sidecar",
        "execution_id": "exec-1",
        "token": "-leading-token",
    }


def test_launcher_prefers_execution_token_env_and_removes_it(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def fake_run(endpoint: str, execution_id: str, token: str) -> int:
        seen["endpoint"] = endpoint
        seen["execution_id"] = execution_id
        seen["token"] = token
        assert "CLAW_EXECUTION_TOKEN" not in os.environ
        return 0

    monkeypatch.setenv("CLAW_EXECUTION_TOKEN", "env-token")
    monkeypatch.setattr(launcher, "run_execution", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        ["claw-launch", "run", "--execution-id", "exec-1"],
    )

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 0
    assert seen == {
        "endpoint": "http://127.0.0.1:8765",
        "execution_id": "exec-1",
        "token": "env-token",
    }


def test_payload_environment_removes_scheduler_credentials(monkeypatch) -> None:
    launcher_path = "/workspace/.claw/scheduler/src"
    original_path = "/task/pythonpath"
    monkeypatch.setenv("CLAW_EXECUTION_TOKEN", "claim-token")
    monkeypatch.setenv("CLAW_SCHEDULER_TOKEN", "legacy-bearer")
    monkeypatch.setenv("OPENCLAW_SCHEDULER_TOKEN", "bearer")
    monkeypatch.setenv("CLAW_LAUNCHER_PYTHONPATH", launcher_path)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((launcher_path, original_path)))
    monkeypatch.setenv("CLAW_TASK_PYTHON", "/opt/conda/bin/python3")
    original_exec_path = os.pathsep.join(("/usr/local/bin", "/usr/bin"))
    monkeypatch.setenv("PATH", original_exec_path)
    monkeypatch.setenv("KEEP", "value")

    env = launcher._payload_environment()

    assert env["KEEP"] == "value"
    assert "CLAW_EXECUTION_TOKEN" not in env
    assert "CLAW_SCHEDULER_TOKEN" not in env
    assert "OPENCLAW_SCHEDULER_TOKEN" not in env
    assert "CLAW_LAUNCHER_PYTHONPATH" not in env
    assert env["PYTHONPATH"] == original_path
    assert env["PATH"] == os.pathsep.join(
        ("/opt/claw/bin", "/opt/conda/bin", "/usr/local/bin", "/usr/bin")
    )


def test_launcher_main_hides_internal_wrapper_errors(monkeypatch, capsys) -> None:
    def fail_run(_endpoint: str, _execution_id: str, _token: str) -> int:
        raise RuntimeError("cgroup_join_failed path=/sys/fs/cgroup/claw token=secret")

    monkeypatch.setattr(launcher, "run_execution", fail_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "claw-launch",
            "run",
            "--execution-id",
            "exec-1",
            "--token=secret",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 125
    captured = capsys.readouterr()
    assert "Command could not be started by the execution environment." in captured.err
    assert "claw-launch" not in captured.err
    assert "cgroup" not in captured.err
    assert "secret" not in captured.err


def test_launcher_main_prints_redacted_debug_when_cgroup_required(monkeypatch, capsys) -> None:
    def fail_run(_endpoint: str, _execution_id: str, _token: str) -> int:
        raise RuntimeError("cgroup_join_failed path=/sys/fs/cgroup/claw token=secret")

    monkeypatch.setenv("CLAW_CGROUP_REQUIRED", "1")
    monkeypatch.setattr(launcher, "run_execution", fail_run)
    monkeypatch.setattr(
        "sys.argv",
        [
            "claw-launch",
            "run",
            "--execution-id",
            "exec-1",
            "--token=secret",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 125
    captured = capsys.readouterr()
    assert "claw-launch debug: RuntimeError: cgroup_join_failed" in captured.err
    assert "token=<redacted>" in captured.err
    assert "secret" not in captured.err
