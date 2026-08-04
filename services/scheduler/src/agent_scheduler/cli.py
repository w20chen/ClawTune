"""Installed-sidecar CLI entry points.

These console scripts are available after ``pip install clawtune-sidecar``.
When the ClawTune repository checkout is available on the same machine the
functions delegate to the full ``scripts/clawtune.py`` bootstrap.  Otherwise
they provide a minimal self-test and guidance.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _clawtune_home() -> Path:
    """Well-known ClawTune data directory (configurable via env)."""
    return Path(
        os.environ.get(
            "CLAWTUNE_HOME",
            str(Path.home() / ".local" / "share" / "clawtune"),
        )
    )


def _venv() -> Path:
    return _clawtune_home() / "venv"


def _repo_root() -> Path | None:
    """Find the ClawTune repository root if available on this machine."""
    # Check common checkout locations
    candidates = [
        Path.cwd(),
        Path.home() / "ClawTune",
        Path.home() / "clawtune",
    ]
    # Also check if running from a checkout where this package is installed -e
    try:
        import agent_scheduler

        pkg_path = Path(agent_scheduler.__file__).resolve().parents[2]
        # If installed -e from services/scheduler/src, parent is services/scheduler
        if (pkg_path / "scripts" / "clawtune.py").exists():
            candidates.insert(0, pkg_path.parents[1])  # services/.. = repo root
    except Exception:
        pass

    for candidate in candidates:
        marker = candidate / "scripts" / "clawtune.py"
        if marker.is_file():
            return candidate.resolve()
    return None


def _run_clawtune(subcommand: str, *extra_args: str) -> int:
    """Delegate to the repository bootstrap script."""
    repo = _repo_root()
    if repo is None:
        print(
            "The ClawTune repository checkout was not found on this machine.",
            file=sys.stderr,
        )
        print(
            "Install the sidecar and plugin separately, then configure OpenClaw:",
            file=sys.stderr,
        )
        print(f"  1. Create a venv:  python3 -m venv --system-site-packages {_venv()}", file=sys.stderr)
        print(f"  2. Install sidecar: {_venv() / 'bin' / 'pip'} install clawtune-sidecar", file=sys.stderr)
        print( "  3. Install plugin:  openclaw plugins install clawhub:@owner/clawtune", file=sys.stderr)
        print(f"  4. Start sidecar:   {_venv() / 'bin' / 'clawtune-sidecar'}", file=sys.stderr)
        return 1

    script = repo / "scripts" / "clawtune.py"
    python = shutil.which("python3") or sys.executable
    result = subprocess.run(
        [python, str(script), subcommand, *extra_args],
        cwd=repo,
    )
    return result.returncode


def setup_main() -> None:
    """Entry point for ``clawtune-setup``."""
    sys.exit(_run_clawtune("setup"))


def doctor_main() -> None:
    """Entry point for ``clawtune-doctor`` — environment diagnostics."""
    repo = _repo_root()
    if repo is not None:
        sys.exit(_run_clawtune("doctor"))

    # Installed-only minimal doctor
    report: dict[str, object] = {
        "clawtune_home": str(_clawtune_home()),
        "venv": {
            "path": str(_venv()),
            "python_exists": (_venv() / "bin" / "python").exists(),
        },
        "commands": {
            name: shutil.which(name)
            for name in ("python3", "docker", "node", "npm", "openclaw", "sudo")
        },
        "platform": platform.platform(),
        "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
    }

    # Check for installed sidecar
    try:
        import agent_scheduler

        report["agent_scheduler"] = {
            "importable": True,
            "version": getattr(agent_scheduler, "__version__", "unknown"),
        }
    except ImportError:
        report["agent_scheduler"] = {"importable": False}

    print(json.dumps(report, indent=2, ensure_ascii=False))

    # Quick health summary
    ok = True
    if not report["cgroup_v2"]:
        print("WARNING: cgroup v2 not available; eBPF attribution will not work.", file=sys.stderr)
        ok = False
    if not report["agent_scheduler"]["importable"]:
        print("WARNING: agent_scheduler is not importable; the sidecar is not installed.", file=sys.stderr)
        ok = False
    sys.exit(0 if ok else 1)


def check_main() -> None:
    """Entry point for ``clawtune-check`` — eBPF validation."""
    sys.exit(_run_clawtune("check"))
