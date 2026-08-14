from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from shutil import which
from typing import Any


_SUBPROCESS_EXIT_REPORT_TIMEOUTS_SECONDS = (0.75, 10.0)
_FORK_EXEC_EXIT_REPORT_TIMEOUTS_SECONDS = (0.75, 0.75, 0.75)
_EXIT_REPORT_RETRY_DELAY_SECONDS = 0.05
_START_REPORT_TIMEOUT_SECONDS = 60.0


def main() -> None:
    parser = argparse.ArgumentParser(prog="clawtune-launch")
    sub = parser.add_subparsers(dest="command_name", required=True)
    run = sub.add_parser("run")
    run.add_argument("--execution-id", required=True)
    run.add_argument("--token", help=argparse.SUPPRESS)
    run.add_argument(
        "--endpoint",
        default=os.environ.get("CLAWTUNE_ENDPOINT") or "http://127.0.0.1:8765",
    )
    sub.add_parser("diagnose")
    args = parser.parse_args()

    if args.command_name == "diagnose":
        diagnostics = launcher_diagnostics()
        print(json.dumps(diagnostics, sort_keys=True))
        raise SystemExit(0 if diagnostics["ready"] else 2)
    if args.command_name == "run":
        token = os.environ.pop("CLAWTUNE_EXECUTION_TOKEN", None) or args.token
        if not token:
            # Degraded mode: the plugin could not register the execution with
            # the sidecar (network/auth error) but still wrapped the command
            # with the launcher.  Run the payload directly with cgroup
            # isolation, skipping sidecar claim/started/exited reporting.
            payload_command = os.environ.pop("CLAWTUNE_PAYLOAD_COMMAND", None)
            if payload_command:
                try:
                    raise SystemExit(_run_degraded(args.endpoint, args.execution_id, payload_command))
                except Exception as exc:
                    print("Command could not be started by the execution environment.", file=sys.stderr)
                    if _env_enabled("CLAWTUNE_LAUNCH_DEBUG") or _env_enabled("CLAWTUNE_CGROUP_REQUIRED"):
                        print(
                            f"clawtune-launch debug: {type(exc).__name__}: {_redact_debug_message(str(exc))}",
                            file=sys.stderr,
                        )
                    raise SystemExit(125) from None
            run.error("CLAWTUNE_EXECUTION_TOKEN is required")
        try:
            raise SystemExit(run_execution(args.endpoint, args.execution_id, token))
        except Exception as exc:
            print("Command could not be started by the execution environment.", file=sys.stderr)
            if _env_enabled("CLAWTUNE_LAUNCH_DEBUG") or _env_enabled("CLAWTUNE_CGROUP_REQUIRED"):
                print(
                    f"clawtune-launch debug: {type(exc).__name__}: {_redact_debug_message(str(exc))}",
                    file=sys.stderr,
                )
            raise SystemExit(125) from None
    raise SystemExit(2)


def run_execution(endpoint: str, execution_id: str, token: str) -> int:
    """Claim and run one execution using the configured launcher strategy.

    The OpenClaw Docker sandbox needs the payload to be a direct child that
    replaces itself with the requested shell command. Keeping that behavior
    opt-in preserves the richer local-host cgroup/placement path for normal
    Linux installations while making the benchmark sandbox deterministic.
    """
    mode = _selected_launch_mode()
    if mode == "fork-exec":
        if not (_supports_posix_controls() and hasattr(os, "fork")):
            raise RuntimeError("CLAWTUNE_LAUNCH_MODE=fork-exec requires POSIX os.fork")
        return _run_forkexec(endpoint, execution_id, token)
    if mode != "subprocess":
        raise ValueError(f"unsupported CLAWTUNE_LAUNCH_MODE: {mode!r}")
    return _run_subprocess(endpoint, execution_id, token)


def _selected_launch_mode() -> str:
    return os.environ.get("CLAWTUNE_LAUNCH_MODE", "subprocess").strip().lower()


def launcher_diagnostics() -> dict[str, Any]:
    mode = _selected_launch_mode()
    fork_supported = _supports_posix_controls() and hasattr(os, "fork")
    payload_env = _payload_environment()
    payload_path = payload_env.get("PATH", "")
    return {
        "mode": mode,
        "fork_supported": fork_supported,
        "ready": mode == "subprocess" or (mode == "fork-exec" and fork_supported),
        "payload_path": payload_path,
        "payload_python3": which("python3", path=payload_path),
        "payload_pip": which("pip", path=payload_path),
        "payload_pip3": which("pip3", path=payload_path),
    }


def _run_forkexec(endpoint: str, execution_id: str, token: str) -> int:
    """Fork+exec path: the child becomes the command, parent reports exit.

    Uses a pipe-based gate to guarantee the sidecar's trusted-root
    registration (including eBPF collector attachment) completes before
    the child execs the payload.  Without this ordering the collector
    may miss the root exec event and produce an attribution_gap.
    """
    launcher_pid = os.getpid()
    claim = _post_json(
        endpoint, "/v2/executions/claim",
        {"execution_id": execution_id, "token": token, "launcher_pid": launcher_pid},
    )
    command = str(claim["command"])
    workdir = claim.get("workdir")
    cwd = str(workdir) if isinstance(workdir, str) and workdir else None
    update_token = str(claim["update_token"])

    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        # ── Child: block until parent registers us, then exec ──────
        os.close(write_fd)
        try:
            if cwd:
                os.chdir(cwd)
            # Wait for the parent to complete /started registration.
            if not _exec_gate_opened(read_fd):
                os._exit(126)
            os.execve(
                "/bin/sh",
                ["/bin/sh", "-c", command],
                _payload_environment(),
            )
        except BaseException as exc:
            if _env_enabled("CLAWTUNE_LAUNCH_DEBUG"):
                print(
                    "clawtune-launch child debug: "
                    f"{type(exc).__name__}: {_redact_debug_message(str(exc))}",
                    file=sys.stderr,
                )
        os._exit(126)

    # ── Parent: register trusted root, then release child ──────────
    os.close(read_fd)
    # Give the forked payload a per-execution cgroup when one can be created
    # (writable cgroupfs).  The child is moved into it *before* the exec gate
    # opens, so its exec and the whole clause run inside the dedicated cgroup
    # and the sidecar gets a usable cgroup-v2 clawtune-launch scope instead of
    # the coarse shared-sandbox-container fallback.  When cgroupfs is read-only
    # (Docker sandbox, remote sidecar), keep the child blocked and ask the
    # privileged host sidecar to create and populate the exclusive cgroup.
    cgroup_path: str | None = None
    local_cgroup_owned = False
    try:
        placement = claim.get("placement")
        profiling = claim.get("profiling")
        cpu_set = _extract_cpu_set(placement)
        mems = _extract_mems(placement)
        cgroup_path = _prepare_cgroup_with_host_fallback(
            execution_id,
            cpu_set,
            mems,
            profiling,
        )
        local_cgroup_owned = _prepared_cgroup_is_launcher_owned(cgroup_path)
        if cgroup_path is not None and not _join_child_cgroup_with_host_fallback(
            pid,
            cgroup_path,
            profiling,
        ):
            # Moving the child failed; never report a cgroup we do not own, or
            # the sampler would misattribute a shared scope as per-execution.
            if local_cgroup_owned:
                _cleanup_cgroup(cgroup_path)
            local_cgroup_owned = False
            cgroup_path = None
        use_host_cgroup_gate = (
            cgroup_path is None and _host_cgroup_gate_enabled(profiling)
        )
        if (
            cgroup_path is None
            and _env_enabled("CLAWTUNE_CGROUP_REQUIRED")
            and not use_host_cgroup_gate
        ):
            raise RuntimeError("cgroup_unavailable: host_cgroup_gate_disabled")
        started_response = _post_started(
            endpoint,
            execution_id=execution_id,
            update_token=update_token,
            launcher_pid=launcher_pid,
            child_pid=pid,
            cgroup_path=cgroup_path,
            host_cgroup_gate=use_host_cgroup_gate,
        )
        if started_response.get("stored") is not True:
            raise RuntimeError(
                "sidecar did not acknowledge the forked execution start"
            )
        if cgroup_path is None:
            response_cgroup = started_response.get("cgroup_path")
            if isinstance(response_cgroup, str) and response_cgroup:
                cgroup_path = response_cgroup
        # Release the child only after successful registration.
        if os.write(write_fd, b"1") != 1:
            raise RuntimeError("failed to release the forked execution gate")
    except BaseException:
        # EOF is a failure signal: the child must exit without executing.
        try:
            os.close(write_fd)
        except OSError:
            pass
        _waitpid_nointr(pid)
        if local_cgroup_owned:
            _cleanup_cgroup(cgroup_path)
        raise
    os.close(write_fd)

    # ── Parent: wait for child, report exit status ─────────────────
    restore_signal_handlers = _install_fork_signal_forwarders(pid)
    try:
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
    except OSError:
        if local_cgroup_owned:
            _cleanup_cgroup(cgroup_path)
        return 1
    finally:
        restore_signal_handlers()
    if os.WIFEXITED(status):
        exit_code = os.WEXITSTATUS(status)
        term_signal = None
    elif os.WIFSIGNALED(status):
        exit_code = None
        term_signal = os.WTERMSIG(status)
    else:
        exit_code = None
        term_signal = None
    _post_json_best_effort(
        endpoint, f"/v2/executions/{execution_id}/exited",
        {"update_token": update_token, "exit_code": exit_code, "signal": term_signal},
    )
    if local_cgroup_owned:
        _cleanup_cgroup(cgroup_path)
    return exit_code if exit_code is not None else _shell_exit_code(-int(term_signal or 1))


def _waitpid_nointr(pid: int) -> int | None:
    """Reap a forked child, retrying interrupted waits."""
    while True:
        try:
            _, status = os.waitpid(pid, 0)
            return status
        except InterruptedError:
            continue
        except OSError:
            return None


def _exec_gate_opened(read_fd: int) -> bool:
    """Return true only for the parent's explicit post-registration release."""
    try:
        return os.read(read_fd, 1) == b"1"
    finally:
        os.close(read_fd)


def _install_fork_signal_forwarders(child_pid: int) -> Callable[[], None]:
    """Forward launcher cancellation to a forked payload and return cleanup."""

    previous: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        try:
            os.kill(child_pid, signum)
        except OSError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        except (OSError, ValueError):
            continue

    def restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass

    return restore


def _run_subprocess(endpoint: str, execution_id: str, token: str) -> int:
    """Subprocess fallback: original spawn-wait-report behavior (cgroup,
    placement, systemd scope support).  Used on Windows and in tests."""
    launcher_pid = os.getpid()
    claim = _post_json(
        endpoint,
        "/v2/executions/claim",
        {"execution_id": execution_id, "token": token, "launcher_pid": launcher_pid},
    )
    command = str(claim["command"])
    workdir = claim.get("workdir")
    cwd = str(workdir) if isinstance(workdir, str) and workdir else None
    update_token = str(claim["update_token"])
    placement = claim.get("placement")
    profiling = claim.get("profiling")
    cpu_set = _extract_cpu_set(placement)
    mems = _extract_mems(placement)
    cgroup_path = _prepare_cgroup_with_host_fallback(
        execution_id,
        cpu_set,
        mems,
        profiling,
    )
    parsed_affinity = _parse_cpu_list(cpu_set) if _enabled(profiling, "enable_affinity", True) else set()
    affinity_cpus = parsed_affinity or None
    started_reported = False

    def report_started(
        child_pid: int,
        child_cgroup_path: str | None,
        *,
        host_cgroup_gate: bool,
    ) -> dict[str, Any]:
        nonlocal started_reported
        response = _post_started(
            endpoint,
            execution_id=execution_id,
            update_token=update_token,
            launcher_pid=launcher_pid,
            child_pid=child_pid,
            cgroup_path=child_cgroup_path,
            host_cgroup_gate=host_cgroup_gate,
        )
        if response.get("stored") is not True:
            raise RuntimeError(
                "sidecar did not acknowledge the execution start"
            )
        started_reported = True
        return response

    release_gate: Callable[[bool], None] | None = None
    use_host_cgroup_gate = cgroup_path is None and _host_cgroup_gate_enabled(profiling)
    # eBPF is armed by the /started request.  A normal Popen child can
    # exec (and for short commands even exit) while the sidecar is still
    # compiling/attaching BPF.  Gate every profiled POSIX payload after a
    # lightweight wrapper has entered the final cgroup, then release it only
    # after /started returns.  The wrapper itself has already execed, so Popen
    # does not deadlock waiting for a blocking preexec_fn.
    # Every managed-wrapper invocation reaches this path. Gate all POSIX
    # payloads, including profiling.mode=off decisions: eBPF is a required
    # execution boundary, not an optional profiler selected by that field.
    use_payload_gate = _supports_posix_controls()
    if use_payload_gate:
        child, release_gate = _spawn_shell_gated(
            command,
            cwd,
            cgroup_path=cgroup_path,
            affinity_cpus=affinity_cpus,
        )
    else:
        child = _spawn_shell(command, cwd, cgroup_path=cgroup_path, affinity_cpus=affinity_cpus)
    cgroup_owned = _prepared_cgroup_is_launcher_owned(cgroup_path)
    try:
        if cgroup_path is not None and not _join_child_cgroup_with_host_fallback(
            child.pid,
            cgroup_path,
            profiling,
        ):
            fallback = _restart_in_systemd_scope(
                child, command, cwd,
                execution_id=execution_id,
                affinity_cpus=affinity_cpus, profiling=profiling,
                on_cgroup_ready=lambda pid, path: report_started(
                    pid, path, host_cgroup_gate=False,
                ),
            )
            if fallback is None:
                # The child never entered the cgroup we created. Do not report
                # that stale path to the sidecar because it may misattribute or
                # lose telemetry. Remote sidecars resolve the host scope through
                # the gate. Never substitute a shared login/session cgroup:
                # that would mix unrelated host processes into this execution's
                # telemetry.
                if cgroup_owned:
                    _cleanup_cgroup(cgroup_path)
                cgroup_owned = False
                use_host_cgroup_gate = _host_cgroup_gate_enabled(profiling)
                cgroup_path = None
            else:
                if cgroup_owned:
                    _cleanup_cgroup(cgroup_path)
                if release_gate is not None:
                    release_gate(False)
                    release_gate = None
                child, cgroup_path = fallback
                cgroup_owned = False
    except Exception:
        if release_gate is not None:
            release_gate(False)
            release_gate = None
        _terminate_child_best_effort(child)
        if cgroup_owned:
            _cleanup_cgroup(cgroup_path)
        raise
    try:
        if (
            cgroup_path is None
            and _env_enabled("CLAWTUNE_CGROUP_REQUIRED")
            and not use_host_cgroup_gate
        ):
            raise RuntimeError("cgroup_unavailable: host_cgroup_gate_disabled")
        _install_signal_forwarders(child)
        started_response = (
            {}
            if started_reported
            else report_started(
                child.pid,
                cgroup_path,
                host_cgroup_gate=use_host_cgroup_gate,
            )
        )
        if cgroup_path is None and isinstance(started_response, dict):
            response_cgroup = started_response.get("cgroup_path")
            if isinstance(response_cgroup, str) and response_cgroup:
                cgroup_path = response_cgroup
        if release_gate is not None:
            release_gate(True)
            release_gate = None
    except Exception:
        if release_gate is not None:
            release_gate(False)
            release_gate = None
        _terminate_child_best_effort(child)
        if cgroup_owned:
            _cleanup_cgroup(cgroup_path)
        raise
    returncode = child.wait()
    exit_code = returncode if returncode >= 0 else None
    term_signal = -returncode if returncode < 0 else None
    _post_json_best_effort(
        endpoint, f"/v2/executions/{execution_id}/exited",
        {"update_token": update_token, "exit_code": exit_code, "signal": term_signal},
    )
    _cleanup_cgroup(cgroup_path) if cgroup_owned else None
    return _shell_exit_code(returncode)


def _run_degraded(endpoint: str, execution_id: str, command: str) -> int:
    """Run a payload command with cgroup isolation but without sidecar auth.

    Used when the plugin's execution registration failed (network/auth error)
    but failOpen is active.  The launcher still creates a cgroup for resource
    isolation and monitoring; it skips claim/started/exited reporting since
    there is no valid sidecar token.

    Returns the shell exit code of the payload command.
    """
    cwd = os.environ.pop("CLAWTUNE_EXEC_WORKDIR", None) or None
    # In degraded mode we always attempt cgroup isolation since that is the
    # primary reason for wrapping the command with the launcher.  Placement
    # (cpu_set / mems) is unavailable without a sidecar claim.
    profiling: dict[str, Any] = {"enable_cgroup": True}
    cgroup_path = _prepare_cgroup(execution_id, None, None, profiling)
    cgroup_owned = cgroup_path is not None

    affinity_cpus: set[int] | None = None
    if _supports_posix_controls():
        child, release_gate = _spawn_shell_gated(
            command, cwd, cgroup_path=cgroup_path, affinity_cpus=affinity_cpus,
        )
    else:
        child = _spawn_shell(command, cwd, cgroup_path=cgroup_path, affinity_cpus=affinity_cpus)
        release_gate = None

    try:
        if cgroup_owned:
            if not _join_child_cgroup(child.pid, cgroup_path):
                fallback = _restart_in_systemd_scope(
                    child, command, cwd,
                    execution_id=execution_id,
                    affinity_cpus=affinity_cpus, profiling=profiling,
                    on_cgroup_ready=None,
                )
                if fallback is None:
                    _cleanup_cgroup(cgroup_path)
                    cgroup_owned = False
                    cgroup_path = None
                else:
                    _cleanup_cgroup(cgroup_path)
                    if release_gate is not None:
                        release_gate(False)
                        release_gate = None
                    child, cgroup_path = fallback
                    cgroup_owned = False
        if release_gate is not None:
            release_gate(True)
            release_gate = None
    except Exception:
        if release_gate is not None:
            release_gate(False)
            release_gate = None
        _terminate_child_best_effort(child)
        if cgroup_owned:
            _cleanup_cgroup(cgroup_path)
        raise
    returncode = child.wait()
    _cleanup_cgroup(cgroup_path) if cgroup_owned else None
    return _shell_exit_code(returncode)


def _spawn_shell(
    command: str,
    cwd: str | None,
    *,
    cgroup_path: str | None = None,
    affinity_cpus: set[int] | None = None,
) -> subprocess.Popen[bytes]:
    if _supports_posix_controls():
        return subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            env=_payload_environment(),
            preexec_fn=_child_preexec(cgroup_path, affinity_cpus),
        )
    return subprocess.Popen(command, cwd=cwd, env=_payload_environment(), shell=True)


def _post_started(
    endpoint: str,
    *,
    execution_id: str,
    update_token: str,
    launcher_pid: int,
    child_pid: int,
    cgroup_path: str | None,
    host_cgroup_gate: bool,
) -> dict[str, Any]:
    # The payload is still behind its gate. A rejected or unreachable
    # /started request means the required collector was not armed, so this
    # lifecycle boundary must fail closed rather than executing unobserved.
    return _post_json(
        endpoint,
        f"/v2/executions/{execution_id}/started",
        {
            "update_token": update_token,
            "launcher_pid": launcher_pid,
            "child_pid": child_pid,
            "process_starttime_ticks": _read_pid_starttime_ticks(child_pid),
            "cgroup_path": cgroup_path,
            "pid_namespace_inode": _pid_namespace_inode(child_pid),
            "container_id": _detect_container_id(),
            "host_cgroup_gate": host_cgroup_gate,
            "cgroup_required": _env_enabled("CLAWTUNE_CGROUP_REQUIRED"),
        },
    )


def _spawn_shell_gated(
    command: str,
    cwd: str | None,
    *,
    cgroup_path: str | None = None,
    affinity_cpus: set[int] | None = None,
) -> tuple[subprocess.Popen[bytes], Callable[[bool], None]]:
    if not _supports_posix_controls():
        return (
            _spawn_shell(
                command,
                cwd,
                cgroup_path=cgroup_path,
                affinity_cpus=affinity_cpus,
            ),
            lambda _allow=True: None,
        )
    env = _payload_environment()
    env["CLAWTUNE_GATED_PAYLOAD"] = command
    read_fd, write_fd = os.pipe()
    wrapper = (
        f"IFS= read -r _claw_release < /proc/self/fd/{read_fd} || exit 125; "
        '[ "$_claw_release" = "clawtune-release-v1" ] || exit 125; '
        '_claw_payload="$CLAWTUNE_GATED_PAYLOAD"; '
        "unset CLAWTUNE_GATED_PAYLOAD; "
        'exec /bin/sh -c "$_claw_payload"'
    )
    try:
        child = subprocess.Popen(
            ["/bin/sh", "-c", wrapper],
            cwd=cwd,
            env=env,
            preexec_fn=_child_preexec(cgroup_path, affinity_cpus),
            pass_fds=(read_fd,),
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(read_fd)
    released = False

    def release(allow: bool = True) -> None:
        nonlocal released
        if released:
            return
        released = True
        if allow:
            try:
                os.write(write_fd, b"clawtune-release-v1\n")
            except OSError:
                pass
        try:
            os.close(write_fd)
        except OSError:
            pass

    return child, release


def _restart_in_systemd_scope(
    child: subprocess.Popen[bytes],
    command: str,
    cwd: str | None,
    *,
    execution_id: str,
    affinity_cpus: set[int] | None,
    profiling: object,
    on_cgroup_ready: Callable[[int, str], None] | None = None,
) -> tuple[subprocess.Popen[bytes], str] | None:
    if not _systemd_scope_fallback_enabled(profiling):
        return None
    if not _supports_posix_controls() or which("systemd-run") is None:
        return None
    # `systemd-run --user` prints a D-Bus error into the tool result when an
    # SSH/cron/container session has no user manager. Probe the same manager
    # interface quietly before suspending the real child or spawning a scope.
    if not _systemd_user_manager_available():
        return None
    unit = f"clawtune-{_safe_execution_id(execution_id)}.scope"
    suspended = _suspend_child_best_effort(child)
    cgroup_probe = tempfile.NamedTemporaryFile(prefix="clawtune-cgroup-", delete=False)
    cgroup_probe.close()
    release_probe = tempfile.NamedTemporaryFile(prefix="clawtune-release-", delete=False)
    release_path = release_probe.name
    release_probe.close()
    _unlink_best_effort(release_path)
    wrapper = (
        'cg="$(awk -F: \'$1=="0"{print $3}\' /proc/self/cgroup)"; '
        'if [ -n "$cg" ] && [ "$cg" != "/" ]; then '
        'printf "%s" "/sys/fs/cgroup$cg" > "$CLAWTUNE_SYSTEMD_CGROUP_FILE"; '
        'else printf "%s" "/sys/fs/cgroup" > "$CLAWTUNE_SYSTEMD_CGROUP_FILE"; fi; '
        'i=0; while [ ! -e "$CLAWTUNE_SYSTEMD_RELEASE_FILE" ] && [ "$i" -lt 650 ]; do '
        'i=$((i+1)); sleep 0.1; done; '
        'IFS= read -r _claw_release < "$CLAWTUNE_SYSTEMD_RELEASE_FILE" || exit 125; '
        'rm -f "$CLAWTUNE_SYSTEMD_RELEASE_FILE"; '
        '[ "$_claw_release" = "clawtune-release-v1" ] || exit 125; '
        'exec /bin/sh -c "$CLAWTUNE_SYSTEMD_PAYLOAD"'
    )
    env = _payload_environment()
    env["CLAWTUNE_SYSTEMD_PAYLOAD"] = command
    env["CLAWTUNE_SYSTEMD_CGROUP_FILE"] = cgroup_probe.name
    env["CLAWTUNE_SYSTEMD_RELEASE_FILE"] = release_path
    args = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "-p",
        "Delegate=yes",
        "/bin/sh",
        "-c",
        wrapper,
    ]
    try:
        fallback_child = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            preexec_fn=_child_preexec(None, affinity_cpus),
        )
    except OSError:
        if suspended:
            _resume_child_best_effort(child)
        _unlink_best_effort(cgroup_probe.name)
        _unlink_best_effort(release_path)
        return None
    cgroup_path = _read_cgroup_probe(cgroup_probe.name) or _systemd_unit_cgroup_path(unit)
    _unlink_best_effort(cgroup_probe.name)
    if cgroup_path is None:
        _terminate_child_best_effort(fallback_child)
        if suspended:
            _resume_child_best_effort(child)
        _unlink_best_effort(release_path)
        return None
    if on_cgroup_ready is not None:
        try:
            on_cgroup_ready(fallback_child.pid, cgroup_path)
        except Exception:
            _write_systemd_gate_best_effort(release_path, "clawtune-abort-v1")
            _terminate_child_best_effort(fallback_child)
            _stop_systemd_unit_best_effort(unit)
            if suspended:
                _resume_child_best_effort(child)
            _unlink_best_effort(release_path)
            raise
    try:
        _write_systemd_gate(release_path, "clawtune-release-v1")
    except Exception:
        _terminate_child_best_effort(fallback_child)
        _stop_systemd_unit_best_effort(unit)
        if suspended:
            _resume_child_best_effort(child)
        _unlink_best_effort(release_path)
        raise
    if suspended:
        _resume_child_best_effort(child)
    _terminate_child_best_effort(child)
    return fallback_child, cgroup_path


def _systemd_scope_fallback_enabled(profiling: object) -> bool:
    raw = os.environ.get("CLAWTUNE_CGROUP_AUTO_SYSTEMD")
    if raw is not None:
        return raw.lower() not in {"0", "false", "no", "off"}
    return _enabled(profiling, "enable_cgroup", False)


def _systemd_user_manager_available() -> bool:
    if which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            check=False,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _host_cgroup_gate_enabled(profiling: object) -> bool:
    raw = os.environ.get("CLAWTUNE_HOST_CGROUP_GATE")
    if raw is not None:
        return raw.lower() not in {"0", "false", "no", "off"}
    # The privileged sidecar can create an exclusive execution cgroup even
    # when an unprivileged local launcher cannot. This is required for direct
    # host execution and is equally valid for local and remote endpoints.
    return _supports_posix_controls()


def _prepare_cgroup_with_host_fallback(
    execution_id: str,
    cpu_set: str | None,
    mems: str | None,
    profiling: object,
) -> str | None:
    """Try launcher-side isolation, deferring strict failure to the host gate.

    ``CLAWTUNE_CGROUP_REQUIRED=1`` normally makes ``_prepare_cgroup`` fail
    immediately when the launcher's cgroup namespace is read-only.  A remote
    privileged sidecar is another valid creator, so keep the payload gated and
    let ``/started`` enforce the same requirement on the host instead.
    """

    try:
        return _prepare_cgroup(execution_id, cpu_set, mems, profiling)
    except RuntimeError as exc:
        if (
            str(exc).startswith("cgroup_unavailable:")
            and _host_cgroup_gate_enabled(profiling)
        ):
            return None
        raise


def _prepared_cgroup_is_launcher_owned(cgroup_path: str | None) -> bool:
    """Distinguish a created child cgroup from explicit or borrowed scopes."""

    if cgroup_path is None or _explicit_cgroup_path() is not None:
        return False
    borrowed = _read_self_cgroup_path()
    return borrowed is None or os.path.normpath(borrowed) != os.path.normpath(
        cgroup_path
    )


def _join_child_cgroup_with_host_fallback(
    child_pid: int,
    cgroup_path: str,
    profiling: object,
) -> bool:
    """Join and verify a local cgroup, or preserve the host-gate fallback."""

    try:
        if not _join_child_cgroup(child_pid, cgroup_path):
            return False
        _verify_child_cgroup(child_pid, cgroup_path)
        return True
    except RuntimeError as exc:
        if (
            str(exc).startswith(
                ("cgroup_join_failed", "cgroup_join_missing", "cgroup_verify_failed")
            )
            and _host_cgroup_gate_enabled(profiling)
        ):
            return False
        raise


def _systemd_unit_cgroup_path(unit: str) -> str | None:
    for _attempt in range(20):
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=ControlGroup", "--value"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0:
            control_group = result.stdout.strip()
            if control_group:
                if control_group == "/":
                    return "/sys/fs/cgroup"
                return f"/sys/fs/cgroup{control_group}"
        time.sleep(0.05)
    return None


def _read_cgroup_probe(path: str) -> str | None:
    for _attempt in range(20):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if value:
            return value
        time.sleep(0.05)
    return None


def _unlink_best_effort(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_systemd_gate(path: str, token: str) -> None:
    """Atomically publish a systemd-wrapper gate decision."""

    parent = Path(path).parent
    temporary = tempfile.NamedTemporaryFile(
        prefix="clawtune-release-decision-",
        dir=parent,
        delete=False,
    )
    temporary_path = temporary.name
    try:
        temporary.write(f"{token}\n".encode("utf-8"))
        temporary.flush()
        temporary.close()
        os.replace(temporary_path, path)
    except BaseException:
        temporary.close()
        _unlink_best_effort(temporary_path)
        raise


def _write_systemd_gate_best_effort(path: str, token: str) -> None:
    try:
        _write_systemd_gate(path, token)
    except Exception:
        pass


def _stop_systemd_unit_best_effort(unit: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            check=False,
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _child_preexec(cgroup_path: str | None, affinity_cpus: set[int] | None):
    def preexec() -> None:
        # Signal forwarding targets the child's process group. Give each
        # payload its own session so forwarding can never hit OpenClaw or the
        # launcher itself when they inherited the same terminal group.
        os.setsid()
        if cgroup_path:
            try:
                _write_file(Path(cgroup_path) / "cgroup.procs", str(os.getpid()))
            except OSError:
                pass
        if affinity_cpus and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(0, affinity_cpus)
            except OSError:
                pass

    return preexec


def _install_signal_forwarders(child: subprocess.Popen[bytes]) -> None:
    if not _supports_posix_controls():
        return

    def forward(signum: int, _frame: object) -> None:
        try:
            child_pgid = _exclusive_child_pgid(child)
            if child_pgid is not None:
                os.killpg(child_pgid, signum)
            else:
                os.kill(child.pid, signum)
        except OSError:
            pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)


def _post_json_best_effort(endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if path.endswith("/exited"):
        # Exit delivery closes the eBPF collector.  Try the normal fast local
        # path first, then allow one bounded wait for a concurrent cold
        # eBPF attach that is synchronously occupying the sidecar event
        # loop.  The opaque update token stays only in the request body and is
        # never included in diagnostics.
        timeout_budget = (
            _FORK_EXEC_EXIT_REPORT_TIMEOUTS_SECONDS
            if _selected_launch_mode() == "fork-exec"
            else _SUBPROCESS_EXIT_REPORT_TIMEOUTS_SECONDS
        )
        for attempt, timeout_seconds in enumerate(timeout_budget):
            try:
                return _post_json_with_timeout(
                    endpoint,
                    path,
                    payload,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                if attempt + 1 < len(timeout_budget):
                    time.sleep(_EXIT_REPORT_RETRY_DELAY_SECONDS)
        return {}
    try:
        return _post_json(endpoint, path, payload)
    except Exception:
        return {}


def _post_json(endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    timeout_seconds = (
        _start_report_timeout_seconds()
        if path.endswith("/started")
        else 10.0
    )
    return _post_json_with_timeout(
        endpoint,
        path,
        payload,
        timeout_seconds=timeout_seconds,
    )


def _start_report_timeout_seconds() -> float:
    raw = os.environ.get("CLAWTUNE_EBPF_START_TIMEOUT_SECONDS")
    if raw is None:
        return _START_REPORT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _START_REPORT_TIMEOUT_SECONDS
    if not 1.0 <= value <= 600.0:
        return _START_REPORT_TIMEOUT_SECONDS
    return value


def _post_json_with_timeout(
    endpoint: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + path,
        data=data,
        method="POST",
        headers={"content-type": "application/json"},
    )
    bearer = os.environ.get("CLAWTUNE_TOKEN")
    if bearer:
        request.add_header("authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"sidecar_http_{exc.code}:{detail}") from exc
    return json.loads(raw) if raw else {}


def _read_pid_starttime_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1 :].split()
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _pid_namespace_inode(pid: int) -> int | None:
    try:
        target = os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None
    prefix = "pid:["
    if target.startswith(prefix) and target.endswith("]"):
        try:
            return int(target[len(prefix) : -1])
        except ValueError:
            return None
    return None


def _read_self_cgroup_path() -> str | None:
    """Return the current process's cgroup v2 path, or None.

    Reads /proc/self/cgroup to find the cgroup this process belongs to.
    Returns a full filesystem path (e.g. /sys/fs/cgroup/user.slice/...)
    suitable for reading cgroup stat files.

    This is used as a last-resort fallback in containers where cgroupfs
    is mounted read-only: we cannot create sub-cgroups, but we CAN read
    stats from the container's own cgroup.
    """
    try:
        with open("/proc/self/cgroup", "r") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("0::"):
                    continue
                path = line[3:]  # strip "0::" prefix
                if not path or path == "/":
                    return "/sys/fs/cgroup"
                return f"/sys/fs/cgroup{path}"
    except OSError:
        return None


def _detect_container_id() -> str | None:
    for name in ("CLAWTUNE_SANDBOX_CONTAINER_ID",):
        value = os.getenv(name)
        detected = _normalize_container_id(value)
        if detected is not None:
            return detected
    try:
        detected = _container_id_from_text(Path("/proc/self/cgroup").read_text(encoding="utf-8"))
    except OSError:
        detected = None
    if detected is not None:
        return detected
    return None


def _normalize_container_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return _container_id_from_text(value)


def _container_id_from_text(value: str) -> str | None:
    for part in value.replace("\\", "/").split("/"):
        token = part.strip()
        for prefix in ("docker-", "cri-containerd-", "crio-"):
            if token.startswith(prefix):
                token = token[len(prefix):]
        if token.endswith(".scope"):
            token = token[:-6]
        token = token.strip()
        if _looks_like_container_id(token):
            return token
    return None


def _looks_like_container_id(value: str) -> bool:
    return 12 <= len(value) <= 128 and all(ch in "0123456789abcdef" for ch in value.lower())


def _explicit_cgroup_path() -> str | None:
    raw = os.environ.get("CLAWTUNE_CGROUP_PATH")
    return raw if raw else None


def _prepare_cgroup(
    execution_id: str,
    cpu_set: str | None,
    mems: str | None,
    profiling: object,
) -> str | None:
    """Create a cgroup for execution_id, returning its path or None.

    Tries candidate roots in priority order until one succeeds:
      1. CLAWTUNE_CGROUP_ROOT (env override)
      2. /sys/fs/cgroup/clawtune           (root / pre-delegated)
      3. /sys/fs/cgroup/user.slice/    (systemd, writable by non-root)

    Cgroups are opt-in through profiling.enable_cgroup or CLAWTUNE_CGROUP_REQUIRED=1.
    Set CLAWTUNE_ENABLE_CGROUP=0 to disable automatic cgroup creation.
    Set CLAWTUNE_CGROUP_REQUIRED=1 to fail hard when no root is writable.
    """
    required = _env_enabled("CLAWTUNE_CGROUP_REQUIRED")
    if not _supports_posix_controls():
        if required:
            raise RuntimeError("cgroup_unavailable: posix_controls_unsupported")
        return _explicit_cgroup_path()
    if not required and not _enabled(profiling, "enable_cgroup", False):
        return _explicit_cgroup_path()
    explicit = _explicit_cgroup_path()
    if explicit:
        return explicit

    # Collect candidate roots (env override short-circuits to a single candidate).
    env_root = os.environ.get("CLAWTUNE_CGROUP_ROOT")
    if env_root:
        candidates = [env_root]
    else:
        if not required and os.environ.get("CLAWTUNE_ENABLE_CGROUP", "1") != "1":
            return None
        candidates = _cgroup_root_candidates()

    last_error: str | None = None
    for root in candidates:
        try:
            cgroup_path = _create_cgroup_at(root, execution_id, cpu_set, mems)
        except OSError as exc:
            last_error = str(exc)
            if _env_enabled("CLAWTUNE_CGROUP_DEBUG"):
                print(f"execution environment: cgroup unavailable at {root}: {exc}", file=sys.stderr)
            continue
        # Verify the created cgroup can actually accept processes before
        # committing to it.  On systems where /sys/fs/cgroup/clawtune exists
        # but lacks controller delegation, the directory is created but
        # cgroup.procs writes fail with EACCES.
        if _cgroup_procs_writable(cgroup_path):
            return cgroup_path
        # Clean up the unusable directory and try the next candidate.
        try:
            Path(cgroup_path).rmdir()
        except OSError:
            pass
        last_error = f"cgroup.procs not writable at {cgroup_path}"
        if _env_enabled("CLAWTUNE_CGROUP_DEBUG"):
            print(f"execution environment: skipping unusable cgroup {cgroup_path} (delegation missing)", file=sys.stderr)

    # Last resort: borrow the container's own cgroup for read-only monitoring.
    # In Docker containers cgroupfs is mounted read-only so we cannot create
    # sub-cgroups, but we CAN read cpu.stat / memory.current / io.stat from
    # the container's existing cgroup.  The sidecar sampler only reads; it
    # never writes.
    #
    # When the sidecar runs on a different host (host-openclaw-sandbox mode),
    # the container's cgroup view is not valid on the host side.  Return None
    # and let the sidecar discover the correct host cgroup path independently.
    if not required:
        borrowed = _read_self_cgroup_path()
        if borrowed is not None:
            if _sidecar_is_remote():
                return None
            return borrowed

    if required:
        raise RuntimeError(
            f"cgroup_unavailable: no writable root among {candidates}; last error: {last_error}"
        )
    return None


def _cgroup_root_candidates() -> list[str]:
    """Return candidate cgroup root paths in priority order.

    Priority:
      1. /sys/fs/cgroup/user.slice/.../user@<UID>.service/clawtune
                                                            — systemd user manager
                                                              (properly delegated, writable
                                                               by non-root)
      2. /sys/fs/cgroup/clawtune                               — root or pre-delegated
                                                              (requires manual delegation
                                                               of controllers)

    On many systems the root-level /sys/fs/cgroup/clawtune directory can be
    created but does not have controllers delegated to it, so processes
    cannot be joined.  systemd user slices are reliably pre-delegated
    via PAM / logind and are the preferred target.

    Only candidates whose parent directory already exists and is writable are
    returned.  If the user manager directory is missing (e.g. SSH without PAM),
    we try to start it via D-Bus activation before giving up.
    """
    candidates: list[str] = []

    # Priority 1: systemd user manager slice — reliably delegated.
    try:
        uid = os.getuid()
    except (AttributeError, OSError):
        uid = -1
    if uid > 0:
        user_svc = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"
        if _try_candidate_parent(user_svc):
            candidates.append(f"{user_svc}/clawtune")
        else:
            _start_user_manager()
            if _try_candidate_parent(user_svc):
                candidates.append(f"{user_svc}/clawtune")

    # Priority 2: traditional delegated root.
    if _try_candidate_parent("/sys/fs/cgroup"):
        candidates.append("/sys/fs/cgroup/clawtune")
    elif _try_candidate_parent("/sys/fs/cgroup/clawtune"):
        candidates.append("/sys/fs/cgroup/clawtune")

    return candidates


def _try_candidate_parent(parent_path: str) -> bool:
    """Return True if *parent_path* exists and is writable.

    We only need the parent to be writable so _create_cgroup_at can mkdir
    into it.  The per-execution subdirectory is created on demand.
    """
    try:
        st = os.stat(parent_path)
        if not stat.S_ISDIR(st.st_mode):
            return False
        return os.access(parent_path, os.W_OK)
    except OSError:
        return False


def _start_user_manager() -> None:
    """Attempt to start systemd --user via D-Bus activation.

    `systemctl --user status` triggers D-Bus activation of the user
    manager if it is not already running.  Idempotent and safe to call
    when the manager is already active.

    Does not raise on failure.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "status"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _create_cgroup_at(
    root: str,
    execution_id: str,
    cpu_set: str | None,
    mems: str | None,
) -> str:
    """Create a per-execution cgroup under *root*.  Raises OSError on failure."""
    root_path = Path(root)
    cgroup_path = root_path / _safe_execution_id(execution_id)
    root_path.mkdir(parents=True, exist_ok=True)
    if cpu_set or mems:
        _enable_cgroup_controller(root_path, "cpuset")
    cgroup_path.mkdir(mode=0o700, exist_ok=True)
    if mems:
        _write_file(cgroup_path / "cpuset.mems", mems)
    if cpu_set:
        _write_file(cgroup_path / "cpuset.cpus", cpu_set)
    return str(cgroup_path)


def _cleanup_cgroup(cgroup_path: str | None) -> None:
    """Remove a per-execution cgroup directory.

    Only cleans up directories created under our managed roots
    (/sys/fs/cgroup/clawtune or user slice).  Explicit CLAWTUNE_CGROUP_PATH
    directories are left alone.
    """
    if not cgroup_path:
        return
    explicit = os.environ.get("CLAWTUNE_CGROUP_PATH")
    if explicit and cgroup_path.startswith(explicit.rstrip("/")):
        # User-provided path — don't touch.
        return
    # Only clean up under our known managed prefixes.
    managed = _cgroup_root_candidates()
    if not any(cgroup_path.startswith(root.rstrip("/")) for root in managed):
        return
    try:
        Path(cgroup_path).rmdir()
    except OSError:
        pass


def _cgroup_procs_writable(cgroup_path: str) -> bool:
    """Return True if we can write to cgroup.procs in *cgroup_path*.

    Creating a cgroup directory does not guarantee it can accept processes;
    the parent must have delegated controllers via cgroup.subtree_control.
    """
    procs = Path(cgroup_path) / "cgroup.procs"
    try:
        if not procs.exists():
            return False
        # Test writability without actually moving a process.
        return os.access(procs, os.W_OK)
    except OSError:
        return False


def _join_child_cgroup(child_pid: int, cgroup_path: str | None) -> bool:
    if not cgroup_path:
        return False
    try:
        _write_file(Path(cgroup_path) / "cgroup.procs", str(child_pid))
        return True
    except OSError as exc:
        if _env_enabled("CLAWTUNE_CGROUP_REQUIRED"):
            details = _cgroup_debug_details(Path(cgroup_path), child_pid)
            raise RuntimeError(
                f"cgroup_join_failed path={cgroup_path} child_pid={child_pid}: {exc}; {details}"
            ) from exc
        return False


def _verify_child_cgroup(child_pid: int, cgroup_path: str | None) -> None:
    if not cgroup_path or not _env_enabled("CLAWTUNE_CGROUP_REQUIRED"):
        return
    procs = Path(cgroup_path) / "cgroup.procs"
    try:
        pids = {int(line.strip()) for line in procs.read_text(encoding="utf-8").splitlines() if line.strip()}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cgroup_verify_failed path={cgroup_path}: {exc}") from exc
    if child_pid not in pids:
        details = _cgroup_debug_details(Path(cgroup_path), child_pid)
        raise RuntimeError(f"cgroup_join_missing path={cgroup_path} child_pid={child_pid}; {details}")


def _terminate_child_best_effort(child: subprocess.Popen[bytes]) -> None:
    try:
        child_pgid = _exclusive_child_pgid(child)
        if child_pgid is not None:
            os.killpg(child_pgid, signal.SIGTERM)
        else:
            child.terminate()
        child.wait(timeout=1)
    except Exception:
        try:
            child_pgid = _exclusive_child_pgid(child)
            if child_pgid is not None:
                os.killpg(child_pgid, signal.SIGKILL)
            else:
                child.kill()
        except Exception:
            pass
        try:
            child.wait(timeout=1)
        except Exception:
            pass


def _exclusive_child_pgid(child: subprocess.Popen[bytes]) -> int | None:
    if not _supports_posix_controls() or not hasattr(os, "getpgid"):
        return None
    try:
        child_pgid = os.getpgid(child.pid)
    except OSError:
        return None
    return child_pgid if child_pgid == child.pid else None


def _suspend_child_best_effort(child: subprocess.Popen[bytes]) -> bool:
    stop_signal = getattr(signal, "SIGSTOP", None)
    if not _supports_posix_controls() or stop_signal is None:
        return False
    try:
        os.kill(child.pid, stop_signal)
        return True
    except OSError:
        return False


def _resume_child_best_effort(child: subprocess.Popen[bytes]) -> None:
    cont_signal = getattr(signal, "SIGCONT", None)
    if not _supports_posix_controls() or cont_signal is None:
        return
    try:
        os.kill(child.pid, cont_signal)
    except OSError:
        pass


def _cgroup_debug_details(cgroup_path: Path, child_pid: int) -> str:
    parent = cgroup_path.parent
    fields = {
        "type": _read_text_one_line(cgroup_path / "cgroup.type"),
        "parent_type": _read_text_one_line(parent / "cgroup.type"),
        "controllers": _read_text_one_line(cgroup_path / "cgroup.controllers"),
        "parent_controllers": _read_text_one_line(parent / "cgroup.controllers"),
        "subtree_control": _read_text_one_line(cgroup_path / "cgroup.subtree_control"),
        "parent_subtree_control": _read_text_one_line(parent / "cgroup.subtree_control"),
        "child_cgroup": _read_text_one_line(Path(f"/proc/{child_pid}/cgroup")),
        "launcher_euid": str(os.geteuid()) if hasattr(os, "geteuid") else None,
        "procs_stat": _path_stat_summary(cgroup_path / "cgroup.procs"),
        "parent_procs_stat": _path_stat_summary(parent / "cgroup.procs"),
    }
    return " ".join(f"{key}={_quote_detail(value)}" for key, value in fields.items())


def _read_text_one_line(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip().replace("\n", "|")
    except OSError:
        return None
    return text or "(empty)"


def _quote_detail(value: str | None) -> str:
    return "-" if value is None else repr(value)


def _path_stat_summary(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return f"mode={stat.S_IMODE(st.st_mode):04o},uid={st.st_uid},gid={st.st_gid}"


def _enable_cgroup_controller(cgroup_path: Path, controller: str) -> None:
    subtree = cgroup_path / "cgroup.subtree_control"
    try:
        _write_file(subtree, f"+{controller}")
    except OSError:
        pass


def _write_file(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _extract_cpu_set(placement: object) -> str | None:
    if not isinstance(placement, dict):
        return None
    for key in ("cpu_set", "cpuSet", "cpus"):
        value = placement.get(key)
        if isinstance(value, str):
            cpus = _parse_cpu_list(value)
            return _format_cpu_set(cpus) if cpus else None
        if isinstance(value, list):
            cpus = {int(item) for item in value if isinstance(item, int) and item >= 0}
            return _format_cpu_set(cpus) if cpus else None
    return None


def _extract_mems(placement: object) -> str | None:
    if not isinstance(placement, dict):
        return None
    for key in ("mems", "numa_nodes", "numaNodes"):
        value = placement.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            nodes = {int(item) for item in value if isinstance(item, int) and item >= 0}
            return _format_cpu_set(nodes) if nodes else None
    node = placement.get("numa_node")
    return str(node) if isinstance(node, int) and node >= 0 else None


def _parse_cpu_list(value: str | None) -> set[int]:
    if not value:
        return set()
    cpus: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_raw, end_raw = part.split("-", 1)
            try:
                start = int(start_raw)
                end = int(end_raw)
            except ValueError:
                continue
            if start >= 0 and end >= start:
                cpus.update(range(start, end + 1))
            continue
        try:
            cpu = int(part)
        except ValueError:
            continue
        if cpu >= 0:
            cpus.add(cpu)
    return cpus


def _format_cpu_set(values: set[int]) -> str:
    if not values:
        return ""
    ordered = sorted(values)
    ranges: list[str] = []
    start = prev = ordered[0]
    for item in ordered[1:]:
        if item == prev + 1:
            prev = item
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = item
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def _enabled(profiling: object, key: str, default: bool) -> bool:
    if not isinstance(profiling, dict):
        return default
    value = profiling.get(key)
    return value if isinstance(value, bool) else default


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def _payload_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "CLAWTUNE_EXECUTION_TOKEN",
        "CLAWTUNE_TOKEN",
    ):
        env.pop(key, None)
    launcher_pythonpath = env.pop("CLAWTUNE_LAUNCHER_PYTHONPATH", None)
    if launcher_pythonpath:
        payload_pythonpath = [
            entry
            for entry in env.get("PYTHONPATH", "").split(os.pathsep)
            if entry and entry != launcher_pythonpath
        ]
        if payload_pythonpath:
            env["PYTHONPATH"] = os.pathsep.join(payload_pythonpath)
        else:
            env.pop("PYTHONPATH", None)
    task_python = env.get("CLAWTUNE_TASK_PYTHON")
    if task_python and os.path.isabs(task_python):
        task_bin = os.path.dirname(task_python)
        current_path = env.get("PATH", "")
        path_entries = current_path.split(os.pathsep) if current_path else []
        preferred = ["/opt/clawtune/bin", task_bin]
        env["PATH"] = os.pathsep.join(
            [*preferred, *[entry for entry in path_entries if entry not in preferred]]
        )
    return env


def _sidecar_is_remote() -> bool:
    """Return True when the sidecar endpoint points to a different host.

    When clawtune-launch runs inside a Docker container and the sidecar is on
    the host (reachable through a host-gateway address), the container's
    cgroup view from /proc/self/cgroup is meaningless to the sidecar.
    The sidecar must discover the host cgroup path independently via
    sandbox-scope discovery or ``docker inspect``.
    """
    endpoint = os.environ.get("CLAWTUNE_ENDPOINT") or ""
    remote_markers = (
        "host.docker.internal",
        "host.containers.internal",
        "gateway.docker.internal",
        "host-gateway",
    )
    return any(marker in endpoint for marker in remote_markers)


def _redact_debug_message(message: str) -> str:
    parts = []
    for item in message.split():
        if item.startswith("token="):
            parts.append("token=<redacted>")
        else:
            parts.append(item)
    return " ".join(parts)


def _safe_execution_id(execution_id: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in execution_id)
    return safe[:128] or "exec"


def _supports_posix_controls() -> bool:
    return os.name == "posix"


def _shell_exit_code(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    return 128 + min(127, -returncode)


if __name__ == "__main__":
    main()
