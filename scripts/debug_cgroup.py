#!/usr/bin/env python3
"""Run a live eBPF/cgroup diagnostic against a Docker exec process."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = ROOT / "services" / "sidecar" / "src"
if str(SIDECAR_SRC) not in sys.path:
    sys.path.insert(0, str(SIDECAR_SRC))


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        capture_output=True,
        text=True,
    )


def cgroup_path_for_pid(pid: int) -> Path:
    for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line.split(":", 2)[2].lstrip("/")
    raise RuntimeError(f"PID {pid} is not attached to cgroup v2")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--container", help="Use an existing container ID or name.")
    cli.add_argument(
        "--image",
        default="alpine:3.20",
        help="Diagnostic image when a container is created.",
    )
    return cli


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    created = False
    container = args.container
    if not container:
        existing = run("docker", "ps", "-q", "--filter", "name=clawtune-srb").stdout.splitlines()
        container = existing[0] if existing else None
    if not container:
        name = f"clawtune-srb-debug-{os.getpid()}"
        container = run(
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            args.image,
            "sleep",
            "600",
        ).stdout.strip()
        created = True

    temp_dir = Path(tempfile.mkdtemp(prefix="clawtune-cgroup-debug-"))
    try:
        raw_pid = run("docker", "inspect", container, "--format", "{{.State.Pid}}").stdout.strip()
        if not raw_pid.isdigit() or int(raw_pid) <= 0:
            raise RuntimeError(f"container is not running: {container}")
        init_pid = int(raw_pid)
        cgroup_path = cgroup_path_for_pid(init_pid)
        print(f"Container: {container}")
        print(f"Init PID: {init_pid}; cgroup: {cgroup_path}")

        from tool_resource.telemetry import (  # noqa: PLC0415
            ClauseTelemetryCollector,
            _container_pid_set,
        )

        artifact_path = temp_dir / "artifacts"
        artifact_path.mkdir()
        collector = ClauseTelemetryCollector(
            container_id=container,
            container_executable="docker",
            repo="openclaw",
            artifact_path=artifact_path,
            cgroup_path=str(cgroup_path),
        )
        try:
            print(
                f"Collector state={collector.state}; "
                f"cgroup inodes={sorted(collector.cgroup_inodes)}"
            )
            time.sleep(0.5)
            token = collector.begin_tool_call("debug-tool", "echo hello_world")
            run("docker", "exec", container, "/bin/sh", "-c", "echo hello_world")
            time.sleep(0.5)
            summary = collector.finish_tool_call(token)
            print(
                f"Telemetry quality={summary.get('telemetry_quality')}; "
                f"clauses={len(summary.get('clauses', []))}"
            )
            event_types: dict[str, int] = {}
            for event in collector._events:
                event_type = str(event.get("type"))
                event_types[event_type] = event_types.get(event_type, 0) + 1
            print(f"Event types: {event_types}")
            pids = _container_pid_set(
                collector._events,
                collector.init_pid,
                cgroup_inodes=collector.cgroup_inodes,
            )
            print(f"Attributed host PIDs: {sorted(pids)}")
        finally:
            collector.finalize()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if created and container:
            run("docker", "rm", "--force", container, check=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"live cgroup diagnostic failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
