"""Run ClawTune's native collector against a gated process in one Linux guest."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_SRC = REPO_ROOT / "services" / "sidecar" / "src"
if str(SIDECAR_SRC) not in sys.path:
    sys.path.insert(0, str(SIDECAR_SRC))

from tool_resource.telemetry import (  # noqa: E402
    ClauseTelemetryCollector,
    _bpf_runtime_diagnostics,
)


DEFAULT_COMMAND = "dd if=/dev/zero of=/dev/null bs=1M count=32768"
UNRELATED_ARGV = ["sleep", "17"]


def _prepare_guest_mounts() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("guest eBPF smoke must run as root")
    Path("/sys/kernel/tracing").mkdir(parents=True, exist_ok=True)
    if not Path("/sys/kernel/tracing/kprobe_events").exists():
        subprocess.run(
            ["mount", "-t", "tracefs", "tracefs", "/sys/kernel/tracing"],
            check=True,
        )
    subprocess.run(
        ["mount", "-o", "remount,rw", "/sys/fs/cgroup"],
        check=True,
    )


def _spawn_gated_shell(command: str) -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(write_fd)
            if os.read(read_fd, 1) != b"1":
                os._exit(125)
            os.close(read_fd)
            os.execv("/bin/sh", ["sh", "-c", command])
        except BaseException:
            os._exit(126)
    os.close(read_fd)
    return pid, write_fd


def _validate_artifact(artifact: dict[str, Any], unrelated_pid: int) -> list[str]:
    errors: list[str] = []
    collector = artifact.get("collector") or {}
    calls = artifact.get("calls") or []
    if collector.get("health") != "healthy":
        errors.append(f"collector health={collector.get('health')!r}")
    if artifact.get("cleanup") != "ok":
        errors.append(f"collector cleanup={artifact.get('cleanup')!r}")
    loss = artifact.get("telemetry_loss_total") or {}
    if int(loss.get("total") or 0) != 0:
        errors.append(f"collector loss={loss.get('total')!r}")
    if len(calls) != 1:
        errors.append(f"expected one call, got {len(calls)}")
        return errors
    call = calls[0]
    if call.get("eligible_for_kb") is not True:
        errors.append(f"call not eligible_for_kb: {call.get('invalid_reasons')!r}")
    clauses = call.get("clauses") or []
    if not clauses:
        errors.append("no clauses captured")
    if not any(float(row.get("peak_cpu_cores") or 0) > 0 for row in clauses):
        errors.append("no positive clause peak_cpu_cores")
    if not any(float(row.get("sampled_peak_rss_mb") or 0) > 0 for row in clauses):
        errors.append("no positive clause sampled_peak_rss_mb")
    observed_pids = {
        int(row.get("host_pid") or 0)
        for row in clauses
        if int(row.get("host_pid") or 0) > 0
    }
    if unrelated_pid in observed_pids:
        errors.append(f"unrelated pid {unrelated_pid} leaked into artifact")
    if any(list(row.get("argv") or []) == UNRELATED_ARGV for row in clauses):
        errors.append(f"unrelated command {UNRELATED_ARGV!r} leaked into artifact")
    return errors


def run_smoke(*, artifact_path: Path, repo: str, command: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "clawtune.guest-ebpf-smoke.v1",
        "artifact_path": str(artifact_path),
        "repo": repo,
        "command": command,
        "bpf_runtime": _bpf_runtime_diagnostics(),
        "ok": False,
    }
    cgroup = Path(f"/sys/fs/cgroup/clawtune-guest-{os.getpid()}")
    child_pid = 0
    release_fd = -1
    unrelated: subprocess.Popen[bytes] | None = None
    collector: ClauseTelemetryCollector | None = None
    try:
        _prepare_guest_mounts()
        cgroup.mkdir()
        child_pid, release_fd = _spawn_gated_shell(command)
        (cgroup / "cgroup.procs").write_text(f"{child_pid}\n", encoding="ascii")
        unrelated = subprocess.Popen(
            UNRELATED_ARGV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        report["cgroup_path"] = str(cgroup)
        report["trusted_root_pid"] = child_pid
        report["unrelated_pid"] = unrelated.pid
        collector = ClauseTelemetryCollector(
            container_id=None,
            container_executable="docker",
            repo=repo,
            artifact_path=artifact_path,
            cgroup_path=str(cgroup),
            trusted_root_pid=child_pid,
        )
        raw_events: list[dict[str, Any]] = []
        original_route_event = collector._source.route_event

        def capture_route_event(_source: Any, event: dict[str, Any]) -> None:
            raw_events.append(dict(event))
            original_route_event(event)

        collector._source.route_event = types.MethodType(
            capture_route_event, collector._source
        )
        token = collector.begin_tool_call("guest-semantic-smoke", command)
        os.write(release_fd, b"1")
        os.close(release_fd)
        release_fd = -1
        _, wait_status = os.waitpid(child_pid, 0)
        child_pid = 0
        return_code = os.waitstatus_to_exitcode(wait_status)
        call = collector.finish_tool_call(
            token,
            replay_response={
                "result": "guest semantic smoke",
                "stderr": "",
                "returncode": return_code,
            },
        )
        report["raw_event_diagnostics"] = {
            "count": len(raw_events),
            "token_started_ns": token.started_ns,
            "reported_ns": time.monotonic_ns(),
            "first_event_ns": min(
                (int(row.get("ts_ns") or 0) for row in raw_events), default=0
            ),
            "last_event_ns": max(
                (int(row.get("ts_ns") or 0) for row in raw_events), default=0
            ),
            "cgroup_ids": sorted(
                {int(row.get("cgroup_id") or 0) for row in raw_events}
            ),
            "pid_namespace_inodes": sorted(
                {int(row.get("pid_namespace_inode") or 0) for row in raw_events}
            ),
            "types": sorted(
                {str(row.get("type") or "unknown") for row in raw_events}
            ),
            "routed_count": len(collector._events),
        }
        collector.finalize(replay_execution="completed")
        collector = None
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        errors = _validate_artifact(artifact, unrelated.pid)
        report.update(
            {
                "return_code": return_code,
                "call": call,
                "collector": artifact.get("collector"),
                "telemetry_quality": artifact.get("telemetry_quality"),
                "collection_validity": artifact.get("collection_validity"),
                "errors": errors,
                "ok": return_code == 0 and not errors,
            }
        )
    except BaseException as exc:
        report["errors"] = [f"{type(exc).__name__}: {exc}"]
    finally:
        if release_fd >= 0:
            os.close(release_fd)
        if child_pid > 0:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)
        if collector is not None:
            try:
                collector.finalize(replay_execution="incomplete")
            except BaseException as exc:
                report.setdefault("errors", []).append(
                    f"collector finalize: {type(exc).__name__}: {exc}"
                )
        if unrelated is not None:
            unrelated.terminate()
            try:
                unrelated.wait(timeout=3)
            except subprocess.TimeoutExpired:
                unrelated.kill()
                unrelated.wait()
        try:
            cgroup.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            report.setdefault("errors", []).append(f"cgroup cleanup: {exc}")
            report["ok"] = False
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--repo", default="clawbox/guest-semantic-smoke")
    parser.add_argument("--command", default=DEFAULT_COMMAND)
    args = parser.parse_args(argv)
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    report = run_smoke(artifact_path=args.artifact, repo=args.repo, command=args.command)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(rendered)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
