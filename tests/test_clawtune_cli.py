from __future__ import annotations

import importlib.util
import json
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


def test_kernel_build_honors_explicit_bcc_source(tmp_path, monkeypatch) -> None:
    configured = tmp_path / "kernel-build"
    configured.mkdir()
    monkeypatch.setenv("BCC_KERNEL_SOURCE", str(configured))

    assert clawtune.kernel_build() == configured.resolve()


def test_bcc_probe_requires_the_real_runtime_api(monkeypatch) -> None:
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(tuple(map(str, command)))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(clawtune, "run", fake_run)

    assert clawtune.python_has_bcc(Path("/usr/bin/python3")) is True
    probe = commands[0][-1]
    assert "import_module" in probe
    assert all(name in probe for name in ("BPF", "PerfSWConfig", "PerfType"))


def test_doctor_does_not_report_ready_without_cgroup_v2(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    venv_python = venv / "bin" / "python"
    venv_python.touch()
    kernel = tmp_path / "kernel-build"
    kernel.mkdir()
    monkeypatch.setattr(clawtune, "VENV", venv)
    monkeypatch.setattr(clawtune, "require_linux", lambda: None)
    monkeypatch.setattr(clawtune, "kernel_build", lambda: kernel)
    monkeypatch.setattr(clawtune, "bcc_pythons", lambda: [venv_python])
    monkeypatch.setattr(clawtune, "python_has_bcc", lambda _python: True)
    monkeypatch.setattr(clawtune, "required_commands", lambda: [])
    monkeypatch.setattr(clawtune, "cgroup_v2_available", lambda: False)
    monkeypatch.setattr(clawtune, "sidecar_health", lambda: {"running": False})
    monkeypatch.setattr(clawtune.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert clawtune.doctor() == 1
    assert '"cgroup_v2"' in capsys.readouterr().out


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
    original = (
        '{"plugins": {"load": {"paths": ['
        '"/home/user/claw/packages/openclaw-plugin", "/opt/another-plugin"'
        ']}}}'
    )
    config.write_text(original, encoding="utf-8")
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
            # On retry the stale path has already been removed.
            current = json.loads(config.read_text(encoding="utf-8"))
            assert current["plugins"]["load"]["paths"] == [
                "/opt/another-plugin"
            ]
        return subprocess.CompletedProcess(rendered, 0, "ok", "")

    monkeypatch.setattr(clawtune, "run", fake_run)

    clawtune.install_openclaw_plugin(
        "/usr/bin/openclaw",
        Path("/home/user/ClawTune/packages/openclaw-plugin"),
    )

    assert install_attempts == 2
    # Neither config validate nor doctor --fix should be invoked;
    # we retry the install directly after removing the stale path.
    assert ("/usr/bin/openclaw", "config", "validate") not in calls
    assert ("/usr/bin/openclaw", "doctor", "--fix") not in calls
    backups = list(tmp_path.glob("openclaw.json.clawtune-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    repaired = json.loads(config.read_text(encoding="utf-8"))
    assert repaired["plugins"]["load"]["paths"] == ["/opt/another-plugin"]


def test_plugin_install_only_retries_once_after_repair(
    tmp_path, monkeypatch,
) -> None:
    """After remove_stale_clawtune_plugin_paths we retry the install
    directly — no `config validate` or `doctor --fix` in between —
    because both can trigger OpenClaw's internal last-known-good
    backup restore, which would reintroduce the stale path."""
    config = tmp_path / "openclaw.json"
    original = (
        '{"plugins": {"load": {"paths": ['
        '"/home/user/claw/packages/openclaw-plugin", "/opt/another-plugin"'
        ']}}}'
    )
    config.write_text(original, encoding="utf-8")
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
    assert ("/usr/bin/openclaw", "config", "validate") not in calls
    assert ("/usr/bin/openclaw", "doctor", "--fix") not in calls
    repaired = json.loads(config.read_text(encoding="utf-8"))
    assert repaired["plugins"]["load"]["paths"] == ["/opt/another-plugin"]


def test_plugin_install_removes_stale_path_even_when_it_exists_on_disk(
    tmp_path, monkeypatch,
) -> None:
    """A stale openclaw-plugin directory that still exists on disk
    (old checkout not deleted) must still be removed from
    plugins.load.paths — otherwise openclaw config validate fails,
    doctor --fix auto-restores a last-known-good backup that also
    contains the stale path, and the repair loop is dead."""

    stale_dir = tmp_path / "old-claw" / "packages" / "openclaw-plugin"
    stale_dir.mkdir(parents=True)
    stale_path = str(stale_dir)

    config = tmp_path / "openclaw.json"
    document = {
        "plugins": {
            "load": {"paths": [stale_path, "/opt/another-plugin"]},
        },
    }
    original = json.dumps(document)
    config.write_text(original, encoding="utf-8")
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
                    f"{stale_path}",
                )
            # On retry the stale path has already been removed.
            current = json.loads(config.read_text(encoding="utf-8"))
            assert current["plugins"]["load"]["paths"] == [
                "/opt/another-plugin"
            ]
        return subprocess.CompletedProcess(rendered, 0, "ok", "")

    monkeypatch.setattr(clawtune, "run", fake_run)

    clawtune.install_openclaw_plugin(
        "/usr/bin/openclaw",
        Path("/home/user/ClawTune/packages/openclaw-plugin"),
    )

    assert install_attempts == 2
    assert ("/usr/bin/openclaw", "config", "validate") not in calls
    assert ("/usr/bin/openclaw", "doctor", "--fix") not in calls
    repaired = json.loads(config.read_text(encoding="utf-8"))
    assert repaired["plugins"]["load"]["paths"] == ["/opt/another-plugin"]


def test_plugin_install_removes_stale_dict_format_path_entries(
    tmp_path, monkeypatch,
) -> None:
    """OpenClaw may store plugins.load.paths entries as objects
    with a `path` key, not as plain strings.  The repair must
    also recognise and remove those."""
    config = tmp_path / "openclaw.json"
    document = {
        "plugins": {
            "load": {
                "paths": [
                    {"path": "/home/user/claw/packages/openclaw-plugin"},
                    "/opt/another-plugin",
                ],
            },
        },
    }
    original = json.dumps(document)
    config.write_text(original, encoding="utf-8")
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
            # On retry, only the string entry should remain.
            current = json.loads(config.read_text(encoding="utf-8"))
            assert current["plugins"]["load"]["paths"] == [
                "/opt/another-plugin"
            ]
        return subprocess.CompletedProcess(rendered, 0, "ok", "")

    monkeypatch.setattr(clawtune, "run", fake_run)

    clawtune.install_openclaw_plugin(
        "/usr/bin/openclaw",
        Path("/home/user/ClawTune/packages/openclaw-plugin"),
    )

    assert install_attempts == 2


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
            return (
                b'{"schema_version":"scheduler.health.v1",'
                b'"service":"clawtune-scheduler","ready":true}'
            )

    monkeypatch.setattr(clawtune, "urlopen", lambda *_args, **_kwargs: Response())

    health = clawtune.sidecar_health()

    assert health["running"] is True
    assert health["status"] == 200


def test_sidecar_health_rejects_an_unrelated_service(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"ready":true}'

    monkeypatch.setattr(clawtune, "urlopen", lambda *_args, **_kwargs: Response())

    health = clawtune.sidecar_health()

    assert health["running"] is False
    assert "compatible ClawTune sidecar" in health["error"]


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


def test_benchmark_preserves_narrow_environment_without_secret_in_argv(
    tmp_path,
    monkeypatch,
) -> None:
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    config = tmp_path / "swe_rebench" / "config.yaml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(clawtune, "ROOT", tmp_path)
    monkeypatch.setattr(clawtune, "VENV", venv)
    monkeypatch.setattr(clawtune, "require_linux", lambda: None)
    monkeypatch.setattr(clawtune, "kernel_build", lambda: Path("/kernel/build"))
    monkeypatch.setattr(clawtune, "host_arch", lambda: "aarch64")
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")
    monkeypatch.setenv("AGENT_TEST_BENCH_ROOT", "/data/bench")
    monkeypatch.delenv("SWE_REBENCH_DOCKER_PLATFORM", raising=False)
    commands = []
    monkeypatch.setattr(
        clawtune,
        "run",
        lambda command, **_kwargs: commands.append(tuple(map(str, command))),
    )

    clawtune.benchmark(["--sample", "1"])

    assert len(commands) == 1
    command = commands[0]
    preserve = next(item for item in command if item.startswith("--preserve-env="))
    names = preserve.split("=", 1)[1].split(",")
    assert "LLM_API_KEY" in names
    assert "AGENT_TEST_BENCH_ROOT" in names
    assert "super-secret-value" not in command
    assert "SWE_REBENCH_DOCKER_PLATFORM=linux/amd64" in command


def test_benchmark_keeps_explicit_platform_override(tmp_path, monkeypatch) -> None:
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    config = tmp_path / "swe_rebench" / "config.yaml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(clawtune, "ROOT", tmp_path)
    monkeypatch.setattr(clawtune, "VENV", venv)
    monkeypatch.setattr(clawtune, "require_linux", lambda: None)
    monkeypatch.setattr(clawtune, "kernel_build", lambda: Path("/kernel/build"))
    monkeypatch.setattr(clawtune, "host_arch", lambda: "aarch64")
    monkeypatch.setenv("SWE_REBENCH_DOCKER_PLATFORM", "linux/arm64")
    commands = []
    monkeypatch.setattr(
        clawtune,
        "run",
        lambda command, **_kwargs: commands.append(tuple(map(str, command))),
    )

    clawtune.benchmark([])

    command = commands[0]
    preserve = next(item for item in command if item.startswith("--preserve-env="))
    assert "SWE_REBENCH_DOCKER_PLATFORM" in preserve.split("=", 1)[1].split(",")
    assert "SWE_REBENCH_DOCKER_PLATFORM=linux/amd64" not in command
    assert "linux/arm64" not in command


def test_openclaw_config_enables_gated_privileged_sidecar(monkeypatch) -> None:
    monkeypatch.setattr(clawtune.shutil, "which", lambda _name: "/usr/bin/openclaw")
    monkeypatch.setattr(clawtune, "install_openclaw_plugin", lambda *_args: None)
    patches = []
    commands = []

    def fake_run(command, **kwargs):
        rendered = tuple(map(str, command))
        commands.append(rendered)
        if rendered[-3:] == ("config", "patch", "--stdin"):
            patches.append(kwargs["input_text"])
        return subprocess.CompletedProcess(rendered, 0, "", "")

    monkeypatch.setattr(clawtune, "run", fake_run)

    clawtune.configure_openclaw()

    assert len(patches) == 1
    import json

    entry = json.loads(patches[0])["plugins"]["entries"]["agent-scheduler"]
    assert entry["config"]["autoStartSidecar"] is True
    assert entry["config"]["sidecarCommand"] == ""
    assert str(clawtune.ROOT) not in entry["config"]["sidecarCommand"]
    assert ("/usr/bin/openclaw", "config", "validate") in commands
