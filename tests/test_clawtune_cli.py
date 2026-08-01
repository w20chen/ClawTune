from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from tools.check_ebpf import run_check


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clawtune.py"
SPEC = importlib.util.spec_from_file_location("clawtune_cli", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
clawtune = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clawtune)


def test_package_manager_prefers_dnf(monkeypatch) -> None:
    monkeypatch.setattr(
        clawtune.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"dnf", "apt-get"} else None,
    )
    assert clawtune.package_manager() == "dnf"


def test_package_manager_uses_apt_when_dnf_is_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        clawtune.shutil,
        "which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )
    assert clawtune.package_manager() == "apt"


def test_runtime_config_accepts_user_facing_ebpf_name() -> None:
    from swe_rebench.config import RuntimeConfig

    config = RuntimeConfig.from_dict(
        {"mode": "host-openclaw-sandbox", "ebpf_required": False}
    )
    assert config.stage2_required is False


def test_user_facing_ebpf_name_wins_over_legacy_name() -> None:
    from swe_rebench.config import RuntimeConfig

    config = RuntimeConfig.from_dict(
        {
            "mode": "host-openclaw-sandbox",
            "ebpf_required": True,
            "stage2_required": False,
        }
    )
    assert config.stage2_required is True


def test_ebpf_check_presents_stable_user_facing_readiness(monkeypatch) -> None:
    monkeypatch.setattr(
        "tools.check_ebpf.run_preflight",
        lambda: {
            "stage2_ready": True,
            "platform": "linux",
            "python": "/repo/.venv/bin/python",
            "bcc_import": {"ok": True, "module": "bpfcc"},
            "kernel_header_roots": ["/lib/modules/current/build"],
            "bpf_runtime": {"cgroup_v2": True},
            "semantic_smoke": {"ok": True},
            "error": None,
        },
    )

    report = run_check()

    assert report["ready"] is True
    assert report["bcc"]["module"] == "bpfcc"
    assert "stage2_ready" not in report


def test_plugin_install_repairs_stale_clawtune_link(tmp_path, monkeypatch) -> None:
    config = tmp_path / "openclaw.json"
    config.write_text('{"plugins": {}}', encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config))

    calls: list[tuple[str, ...]] = []
    install_attempts = 0

    def fake_run(command, **_kwargs):
        nonlocal install_attempts
        rendered = tuple(str(item) for item in command)
        calls.append(rendered)
        if "plugins" in rendered and "install" in rendered:
            install_attempts += 1
            if install_attempts == 1:
                return subprocess.CompletedProcess(
                    rendered,
                    1,
                    "",
                    "plugins.load.paths: plugin: plugin path not found: "
                    "/home/user/claw/packages/openclaw-plugin",
                )
        return subprocess.CompletedProcess(rendered, 0, "ok", "")

    monkeypatch.setattr(clawtune, "run", fake_run)

    clawtune.install_openclaw_plugin(
        "/usr/bin/openclaw",
        Path("/home/user/ClawTune/packages/openclaw-plugin"),
    )

    assert install_attempts == 2
    assert ("/usr/bin/openclaw", "doctor", "--fix") in calls
    backups = list(tmp_path.glob("openclaw.json.clawtune-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"plugins": {}}'


def test_plugin_install_does_not_repair_an_unrelated_invalid_config(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        rendered = tuple(str(item) for item in command)
        return subprocess.CompletedProcess(
            rendered,
            1,
            "",
            "plugins.load.paths: plugin path not found: /opt/unrelated/plugin",
        )

    monkeypatch.setattr(clawtune, "run", fake_run)

    try:
        clawtune.install_openclaw_plugin(
            "/usr/bin/openclaw",
            Path("/home/user/ClawTune/packages/openclaw-plugin"),
        )
    except clawtune.SetupError as exc:
        assert "OpenClaw" in str(exc)
    else:
        raise AssertionError("unrelated invalid plugin paths must not be auto-repaired")


def test_sidecar_health_reports_ready_loopback_service(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"status":"ready"}'

    monkeypatch.setattr(clawtune, "urlopen", lambda *_args, **_kwargs: Response())

    health = clawtune.sidecar_health()

    assert health["running"] is True
    assert health["status"] == 200


def test_sidecar_health_explains_connection_refusal(monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(clawtune, "urlopen", refuse)

    health = clawtune.sidecar_health()

    assert health["running"] is False
    assert "connection refused" in health["error"]


def test_agent_reuses_preexisting_sidecar(tmp_path, monkeypatch) -> None:
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    monkeypatch.setattr(clawtune, "VENV", venv)
    monkeypatch.setattr(clawtune, "require_linux", lambda: None)
    monkeypatch.setattr(
        clawtune.shutil,
        "which",
        lambda name: "/usr/bin/openclaw" if name == "openclaw" else None,
    )
    monkeypatch.setattr(
        clawtune,
        "sidecar_health",
        lambda: {"running": True},
    )
    commands = []
    monkeypatch.setattr(
        clawtune,
        "run",
        lambda command, **_kwargs: commands.append(tuple(map(str, command))),
    )

    clawtune.agent(["--local", "--message", "hello"])

    assert commands == [
        ("/usr/bin/openclaw", "agent", "--local", "--message", "hello")
    ]


def test_agent_starts_waits_and_cleans_up_managed_sidecar(tmp_path, monkeypatch) -> None:
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    monkeypatch.setattr(clawtune, "ROOT", tmp_path)
    monkeypatch.setattr(clawtune, "VENV", venv)
    monkeypatch.setattr(clawtune, "require_linux", lambda: None)
    monkeypatch.setattr(
        clawtune.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )
    health = iter(({"running": False}, {"running": True}))
    monkeypatch.setattr(clawtune, "sidecar_health", lambda: next(health))

    class Child:
        pid = 123

        def poll(self):
            return None

    child = Child()
    popen_commands = []

    def fake_popen(command, **_kwargs):
        popen_commands.append(tuple(map(str, command)))
        return child

    monkeypatch.setattr(clawtune.subprocess, "Popen", fake_popen)
    openclaw_commands = []
    monkeypatch.setattr(
        clawtune,
        "run",
        lambda command, **_kwargs: openclaw_commands.append(tuple(map(str, command))),
    )
    stopped = []
    monkeypatch.setattr(clawtune, "stop_managed_sidecar", stopped.append)

    clawtune.agent(["--local", "--message", "hello"])

    assert popen_commands
    assert "sudo" in popen_commands[0]
    assert openclaw_commands == [
        ("/usr/bin/openclaw", "agent", "--local", "--message", "hello")
    ]
    assert stopped == [child]


def test_managed_root_sidecar_is_stopped_through_sudo(tmp_path, monkeypatch) -> None:
    class Child:
        pid = 321

        def poll(self):
            return None

        def wait(self, timeout):
            assert timeout == 5
            return 0

    monkeypatch.setattr(clawtune, "ROOT", tmp_path)
    monkeypatch.setattr(clawtune.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clawtune.shutil, "which", lambda name: f"/usr/bin/{name}")
    commands = []

    def fake_run(command, **_kwargs):
        rendered = tuple(map(str, command))
        commands.append(rendered)
        return subprocess.CompletedProcess(rendered, 0)

    monkeypatch.setattr(clawtune.subprocess, "run", fake_run)

    clawtune.stop_managed_sidecar(Child())

    assert commands == [
        ("sudo", "-n", "/usr/bin/kill", "-TERM", "--", "-321")
    ]
