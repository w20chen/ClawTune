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
