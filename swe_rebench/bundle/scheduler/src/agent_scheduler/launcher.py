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


def main() -> None:
    parser = argparse.ArgumentParser(prog="claw-launch")
    sub = parser.add_subparsers(dest="command_name", required=True)
    run = sub.add_parser("run")
    run.add_argument("--execution-id", required=True)
    run.add_argument("--token", help=argparse.SUPPRESS)
    run.add_argument(
        "--endpoint",
        default=os.environ.get("CLAW_SCHEDULER_ENDPOINT")
        or os.environ.get("OPENCLAW_SCHEDULER_ENDPOINT")
        or "http://127.0.0.1:8765",
    )
    args = parser.parse_args()

    if args.command_name == "run":
        token = os.environ.pop("CLAW_EXECUTION_TOKEN", None) or args.token
        if not token:
            run.error("CLAW_EXECUTION_TOKEN is required")
        try:
            raise SystemExit(run_execution(args.endpoint, args.execution_id, token))
        except Exception as exc:
            print("Command could not be started by the execution environment.", file=sys.stderr)
            if _env_enabled("CLAW_LAUNCH_DEBUG") or _env_enabled("CLAW_CGROUP_REQUIRED"):
                print(
                    f"claw-launch debug: {type(exc).__name__}: {_redact_debug_message(str(exc))}",
                    file=sys.stderr,
                )
            raise SystemExit(125) from None
    raise SystemExit(2)


def run_execution(endpoint: str, execution_id: str, token: str) -> int:
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
    cgroup_path = _prepare_cgroup(execution_id, cpu_set, mems, profiling)
    parsed_affinity = _parse_cpu_list(cpu_set) if _enabled(profiling, "enable_affinity", True) else set()
    affinity_cpus = parsed_affinity or None
    if cgroup_path is not None:
        _post_started(
            endpoint,
            execution_id=execution_id,
            update_token=update_token,
            launcher_pid=launcher_pid,
            child_pid=launcher_pid,
            cgroup_path=cgroup_path,
        )

    child = _spawn_shell(command, cwd, cgroup_path=cgroup_path, affinity_cpus=affinity_cpus)
    # Track whether we own this cgroup (created by us) or are borrowing
    # a pre-existing one (e.g. Docker container's read-only cgroup).
    cgroup_owned = cgroup_path is not None
    try:
        if cgroup_owned:
            if not _join_child_cgroup(child.pid, cgroup_path):
                fallback = _restart_in_systemd_scope(
                    child,
                    command,
                    cwd,
                    execution_id=execution_id,
                    affinity_cpus=affinity_cpus,
                    profiling=profiling,
                    on_cgroup_ready=lambda pid, path: _post_started(
                        endpoint,
                        execution_id=execution_id,
                        update_token=update_token,
                        launcher_pid=launcher_pid,
                        child_pid=pid,
                        cgroup_path=path,
                    ),
                )
                if fallback is None:
                    cgroup_owned = False
                else:
                    _cleanup_cgroup(cgroup_path)
                    child, cgroup_path = fallback
                    cgroup_owned = False
            else:
                _verify_child_cgroup(child.pid, cgroup_path)
    except Exception:
        _terminate_child_best_effort(child)
        if cgroup_owned:
            _cleanup_cgroup(cgroup_path)
        raise
    _install_signal_forwarders(child)
    _post_started(
        endpoint,
        execution_id=execution_id,
        update_token=update_token,
        launcher_pid=launcher_pid,
        child_pid=child.pid,
        cgroup_path=cgroup_path,
    )
    returncode = child.wait()
    exit_code = returncode if returncode >= 0 else None
    term_signal = -returncode if returncode < 0 else None
    _post_json_best_effort(
        endpoint,
        f"/v2/executions/{execution_id}/exited",
        {"update_token": update_token, "exit_code": exit_code, "signal": term_signal},
    )
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
            ["/bin/sh", "-lc", command],
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
) -> None:
    _post_json_best_effort(
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
        },
    )


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
    unit = f"claw-{_safe_execution_id(execution_id)}.scope"
    suspended = _suspend_child_best_effort(child)
    cgroup_probe = tempfile.NamedTemporaryFile(prefix="claw-cgroup-", delete=False)
    cgroup_probe.close()
    release_probe = tempfile.NamedTemporaryFile(prefix="claw-release-", delete=False)
    release_path = release_probe.name
    release_probe.close()
    _unlink_best_effort(release_path)
    wrapper = (
        'cg="$(awk -F: \'$1=="0"{print $3}\' /proc/self/cgroup)"; '
        'if [ -n "$cg" ] && [ "$cg" != "/" ]; then '
        'printf "%s" "/sys/fs/cgroup$cg" > "$CLAW_SYSTEMD_CGROUP_FILE"; '
        'else printf "%s" "/sys/fs/cgroup" > "$CLAW_SYSTEMD_CGROUP_FILE"; fi; '
        'i=0; while [ ! -e "$CLAW_SYSTEMD_RELEASE_FILE" ] && [ "$i" -lt 500 ]; do '
        'i=$((i+1)); sleep 0.01; done; rm -f "$CLAW_SYSTEMD_RELEASE_FILE"; '
        'exec /bin/sh -lc "$CLAW_SYSTEMD_PAYLOAD"'
    )
    env = _payload_environment()
    env["CLAW_SYSTEMD_PAYLOAD"] = command
    env["CLAW_SYSTEMD_CGROUP_FILE"] = cgroup_probe.name
    env["CLAW_SYSTEMD_RELEASE_FILE"] = release_path
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
        "-lc",
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
        on_cgroup_ready(fallback_child.pid, cgroup_path)
    Path(release_path).write_text("1", encoding="utf-8")
    _terminate_child_best_effort(child)
    return fallback_child, cgroup_path


def _systemd_scope_fallback_enabled(profiling: object) -> bool:
    raw = os.environ.get("CLAW_CGROUP_AUTO_SYSTEMD")
    if raw is not None:
        return raw.lower() not in {"0", "false", "no", "off"}
    return _enabled(profiling, "enable_cgroup", False)


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


def _child_preexec(cgroup_path: str | None, affinity_cpus: set[int] | None):
    def preexec() -> None:
        try:
            os.setsid()
        except OSError:
            pass
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
            os.killpg(os.getpgid(child.pid), signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)


def _post_json_best_effort(endpoint: str, path: str, payload: dict[str, Any]) -> None:
    try:
        _post_json(endpoint, path, payload)
    except Exception:
        pass


def _post_json(endpoint: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + path,
        data=data,
        method="POST",
        headers={"content-type": "application/json"},
    )
    bearer = os.environ.get("OPENCLAW_SCHEDULER_TOKEN") or os.environ.get("CLAW_SCHEDULER_TOKEN")
    if bearer:
        request.add_header("authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
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
    for name in (
        "CLAW_SANDBOX_CONTAINER_ID",
        "AGENT_SCHEDULER_SANDBOX_CONTAINER_ID",
    ):
        value = os.getenv(name)
        detected = _normalize_container_id(value)
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
        token = part
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
    raw = os.environ.get("CLAW_CGROUP_PATH")
    return raw if raw else None


def _prepare_cgroup(
    execution_id: str,
    cpu_set: str | None,
    mems: str | None,
    profiling: object,
) -> str | None:
    """Create a cgroup for execution_id, returning its path or None.

    Tries candidate roots in priority order until one succeeds:
      1. CLAW_CGROUP_ROOT (env override)
      2. /sys/fs/cgroup/claw           (root / pre-delegated)
      3. /sys/fs/cgroup/user.slice/    (systemd, writable by non-root)

    Cgroups are opt-in through profiling.enable_cgroup or CLAW_CGROUP_REQUIRED=1.
    Set CLAW_ENABLE_CGROUP=0 to disable automatic cgroup creation.
    Set CLAW_CGROUP_REQUIRED=1 to fail hard when no root is writable.
    """
    required = _env_enabled("CLAW_CGROUP_REQUIRED")
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
    env_root = os.environ.get("CLAW_CGROUP_ROOT")
    if env_root:
        candidates = [env_root]
    else:
        if not required and os.environ.get("CLAW_ENABLE_CGROUP", "1") != "1":
            return None
        candidates = _cgroup_root_candidates()

    last_error: str | None = None
    for root in candidates:
        try:
            return _create_cgroup_at(root, execution_id, cpu_set, mems)
        except OSError as exc:
            last_error = str(exc)
            if _env_enabled("CLAW_CGROUP_DEBUG"):
                print(f"execution environment: cgroup unavailable at {root}: {exc}", file=sys.stderr)

    # Last resort: borrow the container's own cgroup for read-only monitoring.
    # In Docker containers cgroupfs is mounted read-only so we cannot create
    # sub-cgroups, but we CAN read cpu.stat / memory.current / io.stat from
    # the container's existing cgroup.  The sidecar sampler only reads; it
    # never writes.
    if not required:
        borrowed = _read_self_cgroup_path()
        if borrowed is not None:
            return borrowed

    if required:
        raise RuntimeError(
            f"cgroup_unavailable: no writable root among {candidates}; last error: {last_error}"
        )
    return None


def _cgroup_root_candidates() -> list[str]:
    """Return candidate cgroup root paths in priority order.

    Priority:
      1. /sys/fs/cgroup/claw                               — root or pre-delegated
      2. /sys/fs/cgroup/user.slice/.../user@<UID>.service/claw
                                                            — systemd user manager
                                                              (writable by non-root)

    Only candidates whose parent directory already exists and is writable are
    returned.  If the user manager directory is missing (e.g. SSH without PAM),
    we try to start it via D-Bus activation before giving up.
    """
    candidates: list[str] = []

    # Priority 1: traditional delegated root.  In privileged Docker
    # containers this directory may not exist yet, but /sys/fs/cgroup is
    # writable; include it so _create_cgroup_at can create the claw root.
    if _try_candidate_parent("/sys/fs/cgroup"):
        candidates.append("/sys/fs/cgroup/claw")
    elif _try_candidate_parent("/sys/fs/cgroup/claw"):
        candidates.append("/sys/fs/cgroup/claw")

    # Priority 2: systemd user manager slice.
    try:
        uid = os.getuid()
    except (AttributeError, OSError):
        uid = -1
    if uid > 0:
        user_svc = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"
        if _try_candidate_parent(user_svc):
            candidates.append(f"{user_svc}/claw")
        else:
            # User manager might not be running (SSH without PAM, cron, CI).
            # Try D-Bus activation — `systemctl --user` is idempotent and
            # safe to call even when the manager is already active.
            _start_user_manager()
            if _try_candidate_parent(user_svc):
                candidates.append(f"{user_svc}/claw")

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
    (/sys/fs/cgroup/claw or user slice).  Explicit CLAW_CGROUP_PATH
    directories are left alone.
    """
    if not cgroup_path:
        return
    explicit = os.environ.get("CLAW_CGROUP_PATH")
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


def _join_child_cgroup(child_pid: int, cgroup_path: str | None) -> bool:
    if not cgroup_path:
        return False
    try:
        _write_file(Path(cgroup_path) / "cgroup.procs", str(child_pid))
        return True
    except OSError as exc:
        if _env_enabled("CLAW_CGROUP_REQUIRED"):
            details = _cgroup_debug_details(Path(cgroup_path), child_pid)
            raise RuntimeError(
                f"cgroup_join_failed path={cgroup_path} child_pid={child_pid}: {exc}; {details}"
            ) from exc
        return False


def _verify_child_cgroup(child_pid: int, cgroup_path: str | None) -> None:
    if not cgroup_path or not _env_enabled("CLAW_CGROUP_REQUIRED"):
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
        child.terminate()
        child.wait(timeout=1)
    except Exception:
        try:
            child.kill()
        except Exception:
            pass


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
        "CLAW_EXECUTION_TOKEN",
        "CLAW_SCHEDULER_TOKEN",
        "OPENCLAW_SCHEDULER_TOKEN",
    ):
        env.pop(key, None)
    return env


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
