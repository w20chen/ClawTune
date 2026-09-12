"""Resolve Compose without changing the Docker daemon or root's configuration."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from swe_rebench.cancellation import run_command


def _invoking_home() -> Path:
    user = os.environ.get("SUDO_USER")
    if user and user != "root":
        import pwd
        return Path(pwd.getpwnam(user).pw_dir)
    return Path.home()


def compose_argv(*, timeout: float = 30) -> list[str]:
    deadline = time.monotonic() + timeout
    probe = run_command(["docker", "compose", "version"], capture_output=True, text=True, timeout=timeout)
    if probe.returncode == 0:
        return ["docker", "compose"]
    # sudo changes HOME, but the benchmark code and its installed dependencies
    # belong to the invoking user. Use only a fixed executable path, never shell text.
    candidate = _invoking_home() / ".docker/cli-plugins/docker-compose"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        executable = str(candidate.resolve())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired([executable, "version"], timeout)
        run_command([executable, "version"], capture_output=True, text=True, check=True, timeout=remaining)
        return [executable]
    raise RuntimeError("Docker Compose is unavailable; install Compose for Docker or run "
                       "bash scripts/setup/benchmark_cache_dependencies.sh as the benchmark user")
