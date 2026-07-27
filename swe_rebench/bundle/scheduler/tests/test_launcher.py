from __future__ import annotations

import os
from typing import Any

import pytest

from agent_scheduler import launcher


class _FakeChild:
    pid = 4242

    def wait(self) -> int:
        return 7


def test_launcher_claims_starts_and_returns_child_exit_code(monkeypatch) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
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


def test_launcher_prebinds_cgroup_before_spawning_payload(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        events.append(path)
        posts.append((path, payload))
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

    def fake_spawn(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ) -> _FakeChild:
        events.append("spawn")
        assert cgroup_path == str(tmp_path / "exec-1")
        assert affinity_cpus is None
        return _FakeChild()

    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_spawn_shell", fake_spawn)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 7
    assert events[:3] == [
        "/v2/executions/claim",
        "/v2/executions/exec-1/started",
        "spawn",
    ]
    assert posts[1][1]["child_pid"] == posts[1][1]["launcher_pid"]
    assert posts[1][1]["cgroup_path"] == str(tmp_path / "exec-1")
    assert posts[2][1]["child_pid"] == 4242


def test_launcher_extracts_cpu_and_numa_placement() -> None:
    placement = {"cpu_set": "0-2,4", "numa_node": 1}

    assert launcher._extract_cpu_set(placement) == "0-2,4"
    assert launcher._extract_mems(placement) == "1"
    assert launcher._parse_cpu_list("0-2,4") == {0, 1, 2, 4}


def test_launcher_prepares_cgroup_with_cpuset_order(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))

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

    def fake_spawn(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ) -> _FakeChild:
        assert cgroup_path == str(original_cgroup)
        assert affinity_cpus is None
        return _FakeChild()

    def fake_popen(args: list[str], **_kwargs: Any) -> FakeSystemdChild:
        assert args[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
        assert "--unit=claw-exec-1.scope" in args
        assert "Delegate=yes" in args
        return FakeSystemdChild()

    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_spawn_shell", fake_spawn)
    monkeypatch.setattr(launcher, "_join_child_cgroup", lambda _pid, _path: False)
    monkeypatch.setattr(launcher, "_install_signal_forwarders", lambda _child: None)
    monkeypatch.setattr(launcher, "_read_pid_starttime_ticks", lambda _pid: 99)
    monkeypatch.setattr(launcher, "_pid_namespace_inode", lambda _pid: 123)
    monkeypatch.setattr(launcher, "which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "_read_cgroup_probe", lambda _path: systemd_cgroup)
    monkeypatch.setattr(launcher, "_systemd_unit_cgroup_path", lambda _unit: systemd_cgroup)

    assert launcher.run_execution("http://sidecar", "exec-1", "token-1") == 0
    assert posts[2] == (
        "/v2/executions/exec-1/started",
        {
            "update_token": "update-1",
            "launcher_pid": posts[2][1]["launcher_pid"],
            "child_pid": 5151,
            "process_starttime_ticks": 99,
            "cgroup_path": systemd_cgroup,
            "pid_namespace_inode": 123,
            "container_id": None,
        },
    )


def test_launcher_passes_placement_to_spawn(monkeypatch, tmp_path) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post_json(_endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        posts.append((path, payload))
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

    def fake_spawn(
        _command: str,
        _cwd: str | None,
        *,
        cgroup_path: str | None = None,
        affinity_cpus: set[int] | None = None,
    ) -> _FakeChild:
        assert cgroup_path == str(tmp_path / "exec-1")
        assert affinity_cpus == {1, 3}
        return _FakeChild()

    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setenv("CLAW_CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(launcher, "_post_json", fake_post_json)
    monkeypatch.setattr(launcher, "_post_json_best_effort", fake_best_effort)
    monkeypatch.setattr(launcher, "_spawn_shell", fake_spawn)
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
    monkeypatch.setenv("CLAW_EXECUTION_TOKEN", "claim-token")
    monkeypatch.setenv("CLAW_SCHEDULER_TOKEN", "legacy-bearer")
    monkeypatch.setenv("OPENCLAW_SCHEDULER_TOKEN", "bearer")
    monkeypatch.setenv("KEEP", "value")

    env = launcher._payload_environment()

    assert env["KEEP"] == "value"
    assert "CLAW_EXECUTION_TOKEN" not in env
    assert "CLAW_SCHEDULER_TOKEN" not in env
    assert "OPENCLAW_SCHEDULER_TOKEN" not in env


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
