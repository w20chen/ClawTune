from __future__ import annotations

import importlib.util
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
