#!/usr/bin/env python3
"""Inspect the cgroup-v2 identity used by a running Docker container."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "services" / "sidecar" / "src"
if str(SIDECAR_SRC) not in sys.path:
    sys.path.insert(0, str(SIDECAR_SRC))


def docker_output(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def resolve_container(value: str | None) -> str:
    if value:
        return value
    containers = docker_output("ps", "-q", "--filter", "name=clawtune-srb").splitlines()
    if not containers:
        raise RuntimeError("no running clawtune-srb container found; pass a container ID or name")
    return containers[0]


def cgroup_path_for_pid(pid: int) -> Path:
    for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            relative = line.split(":", 2)[2].lstrip("/")
            return Path("/sys/fs/cgroup") / relative
    raise RuntimeError(f"PID {pid} is not attached to a cgroup-v2 hierarchy")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("container", nargs="?", help="Docker container ID or name.")
    cli.add_argument(
        "--pid",
        action="append",
        type=int,
        default=[],
        help="Also print a host PID's cgroup; repeat as needed.",
    )
    return cli


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    container = resolve_container(args.container)
    raw_pid = docker_output("inspect", container, "--format", "{{.State.Pid}}")
    if not raw_pid.isdigit() or int(raw_pid) <= 0:
        raise RuntimeError(f"container is not running: {container}")

    init_pid = int(raw_pid)
    cgroup_path = cgroup_path_for_pid(init_pid)
    cgroup_inode = cgroup_path.stat().st_ino
    print(f"Container: {container}")
    print(f"Init PID: {init_pid}")
    print(f"Cgroup path: {cgroup_path}")
    print(f"Cgroup inode: {cgroup_inode}")

    if cgroup_path.name.startswith("docker-"):
        prefix = cgroup_path.name[: 7 + 32]
        print(f"Sibling cgroups with prefix {prefix!r}:")
        for entry in sorted(cgroup_path.parent.iterdir()):
            if entry.is_dir() and entry.name.startswith(prefix):
                print(f"  {entry.name} inode={entry.stat().st_ino}")

    for pid in args.pid:
        try:
            print(f"PID {pid} cgroup: {cgroup_path_for_pid(pid)}")
        except (FileNotFoundError, PermissionError, RuntimeError) as exc:
            print(f"PID {pid}: {exc}")

    from tool_resource.telemetry import (  # noqa: PLC0415
        _add_sibling_cgroup_inodes,
        _discover_leaf_cgroup_inodes,
    )

    discovered = _discover_leaf_cgroup_inodes(cgroup_path)
    print(f"Discovered leaf cgroup inodes: {sorted(discovered)}")
    sibling_inodes = {cgroup_inode}
    _add_sibling_cgroup_inodes(cgroup_path, sibling_inodes)
    print(f"Container plus sibling cgroup inodes: {sorted(sibling_inodes)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"cgroup diagnostic failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
