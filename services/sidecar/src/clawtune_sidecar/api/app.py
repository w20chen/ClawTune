from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from clawtune_sidecar import __version__ as _sidecar_version
from clawtune_sidecar.api.dependencies import AppState, build_state
from clawtune_sidecar.contracts.models import (
    ExecutionClaimRequest,
    ExecutionClaimResponse,
    ExecutionExitedRequest,
    ExecutionRegistrationRequest,
    ExecutionRegistrationResponse,
    ExecutionScopeResponse,
    ExecutionStartedRequest,
    ExecutionUpdateResponse,
    ModelEvent,
    PlacementAdvice,
    ResourceScope,
    StatusResponse,
    ToolBeforeRequest,
    ToolCompletedEvent,
    ToolDecision,
)
from clawtune_sidecar.llm_proxy import proxy_chat_completions, proxy_models
from clawtune_sidecar.identity import correlation_key, owner_key, owners_compatible
from clawtune_sidecar.monitoring.tool_runtime import ToolRuntimeSample
from clawtune_sidecar.policies.base import SchedulingContext
from clawtune_sidecar.security.auth import verify_bearer


_EBPF_COMPLETION_GRACE_SECONDS = 10.0
_EBPF_ORPHAN_GRACE_SECONDS = 1.0
_OWNED_CGROUP_CLEANUP_GRACE_SECONDS = 10.0
_OWNED_CGROUP_CLEANUP_RETRY_SECONDS = 0.02
_HEALTH_SERVICE = "clawtune-sidecar"
_HEALTH_SCHEMA_VERSION = "clawtune.health.v1"
_PROTOCOL_VERSIONS = ["clawtune.api.v1", "trace.v6", "execution.v1"]
_PROC_ROOT = Path("/proc")
_CGROUP_V2_ROOT = Path("/sys/fs/cgroup")
_AUTHORITATIVE_EXECUTION_ATTRIBUTION_SOURCES = frozenset(
    {"exclusive-execution-cgroup", "trusted-execution-root-pid"}
)


def _sample_summary(sample: ToolRuntimeSample) -> dict[str, object]:
    """Convert a ToolRuntimeSample into a JSON-serializable summary dict."""
    return {
        "tool_call_id": sample.tool_call_id,
        "tool_name": sample.tool_name,
        "duration_ms": sample.duration_ms,
        "resource_class": sample.resource_class,
        "attribution_status": sample.attribution_status,
        "target_pid": sample.target_pid,
        "cpu_time_delta_s": sample.cpu_time_delta_s,
        "rss_bytes_peak": sample.rss_bytes_peak,
    }


def _ambient_before_mb(request: ToolBeforeRequest, sample_rss_bytes: int | None) -> float | None:
    if sample_rss_bytes is not None:
        return sample_rss_bytes / (1024 * 1024)
    if request.tool_name == "exec":
        return 0.0
    return None


def _has_usable_cgroup_scope(scope: ResourceScope | None) -> bool:
    if scope is None or scope.kind != "cgroup-v2":
        return False
    cgroup_path = scope.cgroup_path
    if not cgroup_path:
        return False
    normalized = cgroup_path.replace("\\", "/").rstrip("/")
    return (
        normalized not in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}
        and Path(cgroup_path).is_dir()
    )


def _trusted_cgroup_path(cgroup_path: str | None) -> str | None:
    """Return *cgroup_path* only when it is a real sub-cgroup, not the host root.

    When the launcher runs inside a Docker container with a private cgroup
    namespace and cgroupfs is read-only, the internal fallback produces
    ``/sys/fs/cgroup`` (the host root).  That path is never the correct
    eBPF target because every process belongs to a leaf cgroup whose
    inode differs from the root, so the BPF ``wanted()`` filter would
    silently match zero events.
    """
    if not cgroup_path:
        return None
    normalized = cgroup_path.replace("\\", "/").rstrip("/")
    if normalized in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}:
        return None
    return cgroup_path


def _is_shared_runtime_scope(scope: ResourceScope | None) -> bool:
    return bool(
        scope is not None
        and (
            scope.source == "openclaw-runtime"
            or scope.attribution_source == "shared-runtime-process"
        )
    )


def _is_shared_sandbox_scope(scope: ResourceScope | None) -> bool:
    return bool(
        scope is not None
        and (
            scope.source == "openclaw-sandbox"
            or scope.attribution_source == "shared-sandbox-container"
        )
    )


def _is_authoritative_execution_scope(
    scope: ResourceScope | None,
    execution_id: str | None = None,
) -> bool:
    """Return whether the sidecar authenticated this execution's identity."""

    if scope is None:
        return False
    return bool(
        scope.attribution_source in _AUTHORITATIVE_EXECUTION_ATTRIBUTION_SOURCES
        and (execution_id is None or scope.execution_id == execution_id)
    )


def _profiling_enabled(profiling: object, key: str, default: bool = False) -> bool:
    if not isinstance(profiling, dict):
        return default
    value = profiling.get(key)
    return value if isinstance(value, bool) else default


def _defer_ebpf_until_started(record: object) -> bool:
    request = getattr(record, "request", None)
    return bool(
        request is not None
        and getattr(request, "backend", None) == "managed-wrapper"
        and _profiling_enabled(getattr(request, "profiling", None), "enable_cgroup")
    )


def _completion_reports_running(raw_result: object) -> bool:
    """Return whether OpenClaw yielded a still-running background exec."""

    if not isinstance(raw_result, dict):
        return False
    details = raw_result.get("details")
    if not isinstance(details, dict):
        return False
    status = details.get("status")
    return isinstance(status, str) and status.lower() == "running"


def _safe_cgroup_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return safe[:128] or "execution"


def _pid_starttime_ticks(pid: int) -> int | None:
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
    if target.startswith("pid:[") and target.endswith("]"):
        try:
            return int(target[5:-1])
        except ValueError:
            return None
    return None


def _resolve_host_pid(
    container_pid: int,
    *,
    pid_namespace_inode: int | None,
    starttime_ticks: int | None,
) -> int | None:
    if container_pid <= 0:
        return None
    if _pid_matches(container_pid, pid_namespace_inode, starttime_ticks):
        return container_pid
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        host_pid = int(entry.name)
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        nspid = _status_nspid(status)
        if not nspid or nspid[-1] != container_pid:
            continue
        if _pid_matches(host_pid, pid_namespace_inode, starttime_ticks):
            return host_pid
    return None


def _status_nspid(status: str) -> list[int]:
    for line in status.splitlines():
        if not line.startswith("NSpid:"):
            continue
        values: list[int] = []
        for raw in line.split()[1:]:
            try:
                values.append(int(raw))
            except ValueError:
                return []
        return values
    return []


def _pid_matches(
    host_pid: int,
    pid_namespace_inode: int | None,
    starttime_ticks: int | None,
) -> bool:
    if pid_namespace_inode is not None and _pid_namespace_inode(host_pid) != pid_namespace_inode:
        return False
    if starttime_ticks is not None and _pid_starttime_ticks(host_pid) != starttime_ticks:
        return False
    return True


def _canonical_cgroup_path(
    path: str | Path,
    *,
    cgroup_root: Path = _CGROUP_V2_ROOT,
) -> Path | None:
    """Resolve one non-root path inside the local cgroup-v2 mount."""

    try:
        root = cgroup_root.resolve(strict=True)
        if not (root / "cgroup.controllers").is_file():
            return None
        candidate = Path(path).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    if candidate == root or not candidate.is_dir():
        return None
    return candidate


def _host_cgroup_path_for_pid(
    host_pid: int,
    *,
    proc_root: Path = _PROC_ROOT,
    cgroup_root: Path = _CGROUP_V2_ROOT,
) -> str | None:
    """Derive a host-visible cgroup-v2 path from a verified host PID.

    The launcher-provided path may belong to another cgroup namespace.  The
    privileged sidecar therefore treats ``/proc/<host_pid>/cgroup`` as the
    authority and confirms that the process is still a member before using
    the path for eBPF attribution.
    """

    if host_pid <= 0:
        return None
    try:
        lines = (proc_root / str(host_pid) / "cgroup").read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return None
    relative = next(
        (
            fields[2]
            for line in lines
            if len(fields := line.split(":", 2)) == 3
            and fields[0] == "0"
            and fields[1] == ""
        ),
        None,
    )
    if relative is None:
        return None
    candidate = _canonical_cgroup_path(
        cgroup_root / relative.lstrip("/"),
        cgroup_root=cgroup_root,
    )
    if candidate is None:
        return None
    pid_text = str(host_pid)
    for membership_file in ("cgroup.procs", "cgroup.threads"):
        try:
            members = (candidate / membership_file).read_text(
                encoding="utf-8"
            ).split()
        except OSError:
            continue
        if pid_text in members:
            return str(candidate)
    return None


def _verified_host_execution_scope(
    execution_id: str,
    request: ExecutionStartedRequest,
    host_pid: int,
) -> ResourceScope | None:
    """Build a direct-host scope without trusting a client filesystem path."""

    derived = _host_cgroup_path_for_pid(host_pid)
    if derived is None:
        return None
    if request.cgroup_path is not None:
        # The launcher may report a cgroup path from ITS OWN cgroup namespace
        # (host-openclaw-sandbox: the container's cgroupfs view), which is not
        # host-visible or is rewritten relative to the container cgroup root.
        # /proc/<host_pid>/cgroup is the host authority (the pid was already
        # confirmed a member of the derived path), so only reject when BOTH
        # the claimed and derived paths are host-valid and disagree (that
        # would mean the process moved since the launcher reported its path).
        claimed = _canonical_cgroup_path(request.cgroup_path)
        if claimed is not None and claimed != Path(derived):
            return None
    return ResourceScope(
        kind="cgroup-v2",
        execution_id=execution_id,
        pid=host_pid,
        root_pid=host_pid,
        root_starttime_ticks=_pid_starttime_ticks(host_pid),
        cgroup_path=derived,
        pid_namespace_inode=_pid_namespace_inode(host_pid),
        container_id=None,
        include_children=True,
        source="clawtune-sidecar-host-derived",
        attribution_source="trusted-execution-root-pid",
    )


def _verified_host_pid_scope(
    execution_id: str,
    host_pid: int,
    starttime_ticks: int | None,
) -> ResourceScope:
    """Build a host-PID process-tree scope for per-PID attribution.

    The launcher reports a container-namespace child pid.  After the sidecar
    resolves the matching host pid (pid-namespace + starttime), sampling that
    pid's process tree attributes cpu / memory / disk / network to the payload
    and its descendants instead of the whole shared sandbox container cgroup.
    """
    return ResourceScope(
        kind="pid",
        execution_id=execution_id,
        pid=host_pid,
        root_pid=host_pid,
        process_start_time=None,
        root_starttime_ticks=starttime_ticks,
        cgroup_path=None,
        pid_namespace_inode=None,
        container_id=None,
        include_children=True,
        source="clawtune-sidecar-host-derived",
        attribution_source="trusted-execution-root-pid",
    )


def _is_verified_host_pid_scope(scope: ResourceScope | None) -> bool:
    """True for a host-PID process-tree scope we resolved ourselves.

    Unlike a raw launcher child_pid (container namespace), a verified host pid
    is safe for the host sampler, so the sandbox cgroup fallback must not
    override it.
    """
    return (
        scope is not None
        and scope.kind == "pid"
        and scope.pid is not None
        and scope.attribution_source == "trusted-execution-root-pid"
    )


def _host_execution_cgroup_roots(
    fallback_scope: ResourceScope | None,
    configured_root: str | None,
) -> list[str]:
    """Candidate per-execution cgroup roots, in priority order.

    The sidecar runs on the host and needs a writable cgroup-v2 subtree to
    create per-execution cgroups.  Priority:

      1. the sandbox container's own subtree
         (``<container-cgroup>/clawtune-executions``): this preserves the
         container's hierarchy and is preferred when the container scope has
         already delegated cpu/memory controllers. A populated Docker scope
         commonly cannot enable those domain controllers after the fact, so
         this candidate is verified and skipped when delegation is absent.
      2. root (euid 0): /sys/fs/cgroup/clawtune (root-managed).
      3. non-root: the systemd user slice (pre-delegated via PAM/logind),
         then /sys/fs/cgroup/clawtune.
    An explicit ``configured_root`` short-circuits the search.
    """
    roots: list[str] = []
    if configured_root:
        return [configured_root]
    claw_root = _CGROUP_V2_ROOT / "clawtune"
    if fallback_scope is not None and fallback_scope.cgroup_path:
        # Same-subtree placement avoids a cross-boundary move when the Docker
        # scope was configured with controller delegation. Merely exposing
        # controllers on the parent is not enough; _cgroup_accounting_usable
        # verifies the root's subtree_control before this candidate is used.
        roots.append(
            f"{fallback_scope.cgroup_path.rstrip('/')}/clawtune-executions"
        )
    try:
        euid = os.geteuid()
    except (AttributeError, OSError):
        euid = -1
    if euid == 0:
        # Privileged service: root-managed clawtune cgroup is a natural target.
        roots.append(str(claw_root))
    else:
        # Unprivileged service: systemd user manager slice is pre-delegated.
        if euid > 0:
            roots.append(
                f"/sys/fs/cgroup/user.slice/user-{euid}.slice/"
                f"user@{euid}.service/clawtune-executions"
            )
        if claw_root.exists():
            roots.append(str(claw_root))
    # When the v2 root is available (cgroup.controllers exists), the clawtune
    # cgroup is a valid candidate for both root and unprivileged services
    # (root creates it; a delegated clawtune cgroup is writable by the sidecar).
    if (_CGROUP_V2_ROOT / "cgroup.controllers").is_file():
        clawtune = str(claw_root)
        if clawtune not in roots:
            roots.append(clawtune)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def _enable_cgroup_controllers(root: Path) -> frozenset[str]:
    """Enable required accounting controllers and return the kernel readback.

    ``cpu`` and ``memory`` are enabled together because both are required by
    the strict resource-sampling audit. Optional ``io`` is attempted
    separately so an unavailable optional controller cannot make the required
    operation fail atomically.
    """

    try:
        available = set(
            (root / "cgroup.controllers").read_text(encoding="utf-8").split()
        )
        current = set(
            (root / "cgroup.subtree_control").read_text(encoding="utf-8").split()
        )
    except OSError:
        return frozenset()

    required = {"cpu", "memory"}
    required_additions = (
        [name for name in ("cpu", "memory") if name not in current]
        if required.issubset(available)
        else []
    )
    if required_additions:
        try:
            (root / "cgroup.subtree_control").write_text(
                " ".join(f"+{name}" for name in required_additions),
                encoding="utf-8",
            )
        except OSError:
            pass

    if "io" in available and "io" not in current:
        try:
            (root / "cgroup.subtree_control").write_text(
                "+io",
                encoding="utf-8",
            )
        except OSError:
            pass

    try:
        return frozenset(
            (root / "cgroup.subtree_control").read_text(encoding="utf-8").split()
        )
    except OSError:
        return frozenset()


def _cgroup_accounting_usable(root_path: Path) -> bool:
    """Return whether children receive both required accounting controllers.

    ``cgroup.controllers`` only describes what *could* be enabled. The
    authoritative state is this root's ``cgroup.subtree_control`` after the
    enable attempt. Fail closed when it cannot be read; otherwise a child may
    look exclusive while every cpu/memory sample is unavailable.
    """

    try:
        enabled = set(
            (root_path / "cgroup.subtree_control").read_text(encoding="utf-8").split()
        )
    except OSError:
        return False
    return {"cpu", "memory"}.issubset(enabled)


def _execution_cgroup_accounting_usable(cgroup_path: Path) -> bool:
    """Verify that the execution leaf exposes parsable cpu and memory data."""

    try:
        cpu_stat = (cgroup_path / "cpu.stat").read_text(encoding="utf-8")
        memory_current = int(
            (cgroup_path / "memory.current").read_text(encoding="utf-8").strip()
        )
    except (OSError, ValueError):
        return False
    return memory_current >= 0 and any(
        line.startswith("usage_usec ")
        and line.removeprefix("usage_usec ").strip().isdigit()
        for line in cpu_stat.splitlines()
    )


def _record_cgroup_diag(diagnostics: list[str] | None, message: str) -> None:
    if diagnostics is not None:
        diagnostics.append(message)


def _log_execution_started_decision(
    execution_id: str,
    request: ExecutionStartedRequest,
    trusted_root_pid: int | None,
    scope: ResourceScope | None,
    gate_failed: bool,
    backend: str | None = None,
) -> None:
    """One-line per-execution decision log (captured in sidecar-stderr.txt)."""
    print(
        "execution_started "
        f"id={execution_id} "
        f"gate={request.host_cgroup_gate} "
        f"cgroup_required={request.cgroup_required} "
        f"launcher_cgroup={request.cgroup_path or '-'} "
        f"backend={backend or '-'} "
        f"container={request.container_id or '-'} "
        f"trusted_root_pid={trusted_root_pid} "
        f"gate_failed={gate_failed} "
        f"scope_kind={scope.kind if scope else None} "
        f"scope_source={scope.source if scope else None} "
        f"scope_cgroup={scope.cgroup_path if scope else None}",
        file=sys.stderr,
        flush=True,
    )


def _write_trace_dir_diag(s: AppState, filename: str, lines: list[str]) -> None:
    """Best-effort write of a diagnostics file under the sidecar trace dir."""
    trace_dir = getattr(s.config, "trace_dir", None)
    if not trace_dir:
        return
    try:
        path = Path(trace_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass


def _write_trace_dir_json(
    s: AppState,
    filename: str,
    payload: dict[str, Any],
) -> None:
    """Best-effort write of a JSON diagnostics file under the sidecar trace dir."""
    trace_dir = getattr(s.config, "trace_dir", None)
    if not trace_dir:
        return
    try:
        import json as _json

        path = Path(trace_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass


def _process_tree_pids(host_pid: int) -> list[int]:
    """Return host_pid plus its recursive descendant pids (best-effort)."""
    pids = [host_pid]
    try:
        import psutil
    except ImportError:
        return pids
    try:
        for child in psutil.Process(host_pid).children(recursive=True):
            pid = child.pid
            if pid and pid != host_pid:
                pids.append(pid)
    except Exception:
        pass
    return pids


def _move_pid_tree_into_cgroup(
    host_pid: int,
    cgroup_path: Path,
    *,
    diagnostics: list[str] | None = None,
) -> bool:
    """Move host_pid and its descendants into *cgroup_path* (best-effort).

    The launcher payload forks children before /started; moving only the
    parent would capture just its own future CPU.  Writing each pid to
    ``cgroup.procs`` moves it (children inherit the cgroup at fork), with a
    short retry to absorb fork races. cgroupfs accepts one PID per write; each
    existing child is therefore migrated separately and the gated root is
    written last so future children inherit the execution scope.
    """
    procs_file = cgroup_path / "cgroup.procs"
    for _attempt in range(3):
        pids = _process_tree_pids(host_pid)
        ordered_pids = [pid for pid in pids if pid > 0 and pid != host_pid]
        ordered_pids.append(host_pid)
        for pid in ordered_pids:
            try:
                procs_file.write_text(f"{pid}\n", encoding="utf-8")
            except OSError as exc:
                _record_cgroup_diag(
                    diagnostics,
                    f"cgroup.procs move pid {pid} failed: {exc}",
                )
        members = _cgroup_member_pids(cgroup_path)
        if members is not None and host_pid in members and not any(
            pid not in members and (_PROC_ROOT / str(pid)).exists()
            for pid in ordered_pids
        ):
            return True
        if _attempt < 2:
            time.sleep(0.02)
    return False


def _prepare_host_execution_cgroup(
    execution_id: str,
    request: ExecutionStartedRequest,
    fallback_scope: ResourceScope | None,
    configured_root: str | None,
    diagnostics: list[str] | None = None,
) -> ResourceScope | None:
    host_pid = _resolve_host_pid(
        request.child_pid,
        pid_namespace_inode=request.pid_namespace_inode,
        starttime_ticks=request.process_starttime_ticks,
    )
    if host_pid is None:
        _record_cgroup_diag(
            diagnostics,
            "launcher host pid could not be resolved from child_pid/namespace/starttime",
        )
        return None
    _record_cgroup_diag(diagnostics, f"resolved launcher host pid {host_pid}")
    for root in _host_execution_cgroup_roots(fallback_scope, configured_root):
        root_path = Path(root)
        cgroup_path = root_path / _safe_cgroup_name(execution_id)
        try:
            root_path.mkdir(parents=True, exist_ok=True)
            _enable_cgroup_controllers(root_path)
            if not _cgroup_accounting_usable(root_path):
                # Available controllers are not the same as controllers
                # delegated to children. Reject a false-exclusive scope whose
                # cpu/memory samples would all be unavailable.
                raise PermissionError(
                    f"cpu and memory controllers not delegated at {root}"
                )
            cgroup_path.mkdir(mode=0o700, exist_ok=True)
            if not _execution_cgroup_accounting_usable(cgroup_path):
                raise PermissionError(
                    f"cpu.stat or memory.current unavailable at {cgroup_path}"
                )
            _record_cgroup_diag(diagnostics, f"created per-exec cgroup {cgroup_path}")
        except OSError as exc:
            _record_cgroup_diag(diagnostics, f"root {root}: setup failed: {exc}")
            try:
                cgroup_path.rmdir()
            except OSError:
                pass
            continue
        if not _move_pid_tree_into_cgroup(
            host_pid, cgroup_path, diagnostics=diagnostics
        ):
            _record_cgroup_diag(
                diagnostics,
                f"root {root}: could not move pid {host_pid} tree into {cgroup_path}",
            )
            _cleanup_owned_cgroup(str(cgroup_path))
            continue
        _record_cgroup_diag(
            diagnostics,
            f"root {root}: OK - {cgroup_path} owns launcher pid tree",
        )
        return ResourceScope(
            kind="cgroup-v2",
            execution_id=execution_id,
            pid=host_pid,
            root_pid=host_pid,
            root_starttime_ticks=_pid_starttime_ticks(host_pid),
            cgroup_path=str(cgroup_path),
            pid_namespace_inode=_pid_namespace_inode(host_pid),
            container_id=request.container_id or (
                fallback_scope.container_id if fallback_scope is not None else None
            ),
            include_children=True,
            source="clawtune-sidecar-host-cgroup",
            attribution_source="exclusive-execution-cgroup",
        )
    _record_cgroup_diag(
        diagnostics,
        "no writable per-execution cgroup root succeeded",
    )
    return None


def _host_pid_in_cgroup(host_pid: int, cgroup_path: Path) -> bool:
    members = _cgroup_member_pids(cgroup_path)
    return members is not None and host_pid in members


def _cgroup_member_pids(cgroup_path: Path) -> set[int] | None:
    try:
        raw_members = (cgroup_path / "cgroup.procs").read_text(
            encoding="utf-8"
        ).split()
        return {int(pid) for pid in raw_members}
    except (OSError, ValueError):
        return None


def _cleanup_owned_cgroup(path: str | None) -> bool:
    """Try once to remove an empty owned cgroup.

    Retrying is handled asynchronously by the lifecycle task so a completion
    can cancel cleanup before taking its final snapshot.
    """

    if not path:
        return True
    cgroup = Path(path)
    try:
        if (cgroup / "cgroup.procs").read_text(encoding="utf-8").strip():
            return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        cgroup.rmdir()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def create_app(state: AppState | None = None) -> FastAPI:
    app_state = state or build_state()
    app = FastAPI(title="ClawTune Sidecar", version="0.1.0")
    app.state.sidecar = app_state

    @app.on_event("shutdown")
    async def shutdown_state() -> None:
        cleanup_tasks = list(app_state._owned_cgroup_cleanup_tasks.values())
        app_state._owned_cgroup_cleanup_tasks.clear()
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        app_state.tool_monitor.stop()
        if app_state.docker_exec_observer is not None:
            app_state.docker_exec_observer.stop()
            _write_trace_dir_json(
                app_state,
                "docker_exec_observer_diagnostics.json",
                app_state.docker_exec_observer.diagnostics(),
            )
        if app_state.trace_writer is not None:
            await asyncio.to_thread(app_state.trace_writer.close)
        close_predictor = getattr(app_state.predictor, "close", None)
        if callable(close_predictor):
            await asyncio.to_thread(close_predictor)
        for execution_id in list(app_state._owned_cgroup_paths):
            await cleanup_owned_cgroup(app_state, execution_id)

    def get_state() -> AppState:
        return app.state.sidecar

    def auth(s: AppState = Depends(get_state)) -> None:
        verify_bearer(s.config.auth_token)

    def sandbox_fallback_scope(
        s: AppState,
        runtime_id: str | None = None,
        gateway_id: str | None = None,
    ) -> ResourceScope | None:
        if runtime_id is not None:
            scope = s._sandbox_scopes_by_owner.get((gateway_id, runtime_id))
            if scope is not None:
                return scope
            # A scoped protocol event must never borrow a static/global or
            # another runtime's container while discovery is pending.
            return None
        elif s._sandbox_scope_override is not None:
            return s._sandbox_scope_override
        if not s.config.sandbox_cgroup_path:
            return None
        return ResourceScope(
            kind="cgroup-v2",
            execution_id=None,
            pid=s.config.sandbox_root_pid,
            root_pid=s.config.sandbox_root_pid,
            cgroup_path=s.config.sandbox_cgroup_path,
            container_id=s.config.sandbox_container_id,
            include_children=True,
            source="openclaw-sandbox",
            attribution_source="shared-sandbox-container",
        )

    def sandbox_container_id(
        s: AppState,
        runtime_id: str | None = None,
        gateway_id: str | None = None,
    ) -> str | None:
        scope = sandbox_fallback_scope(s, runtime_id, gateway_id)
        if scope is not None and scope.container_id:
            return scope.container_id
        if s.config.sandbox_container_id and not s._sandbox_scopes_by_owner:
            return s.config.sandbox_container_id
        return None

    def begin_ebpf_for_record(
        s: AppState,
        execution_id: str,
        container_id: str | None,
        cgroup_path: str | None = None,
        trusted_root_pid: int | None = None,
    ) -> bool:
        record = s.executions.get(execution_id)
        if record is None:
            return False
        root_pid = (
            trusted_root_pid
            if trusted_root_pid is not None
            else record.trusted_root_pid
        )
        ebpf_kwargs: dict[str, Any] = {
            "execution_id": execution_id,
            "tool_call_id": record.request.tool_call_id,
            "command": record.request.command,
            "container_id": container_id,
            "repo": record.request.repo or s.config.tool_resource_repo,
        }
        if record.request.gateway_id is not None:
            ebpf_kwargs["gateway_id"] = record.request.gateway_id
        if record.request.runtime_id is not None:
            ebpf_kwargs["runtime_id"] = record.request.runtime_id
        if root_pid is not None:
            ebpf_kwargs["trusted_root_pid"] = root_pid
        if cgroup_path is None:
            return s.predictor.begin_execution(**ebpf_kwargs)
        ebpf_kwargs["cgroup_path"] = cgroup_path
        return s.predictor.begin_execution(**ebpf_kwargs)

    def ebpf_failure_detail(s: AppState, execution_id: str) -> dict[str, object]:
        telemetry = s.predictor.execution_telemetry(execution_id)
        reason = getattr(telemetry, "unavailable_reason", None)
        return {
            "code": "tool_resource_ebpf_start_failed",
            "reason": reason or "collector_start_rejected",
        }

    def cancel_ebpf_fallback(s: AppState, execution_id: str) -> None:
        task = s._ebpf_finalize_tasks.pop(execution_id, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_owned_cgroup_cleanup(s: AppState, execution_id: str) -> None:
        task = s._owned_cgroup_cleanup_tasks.pop(execution_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def cleanup_owned_cgroup(s: AppState, execution_id: str) -> bool:
        """Remove an empty sidecar-owned cgroup without losing final counters."""

        path = s._owned_cgroup_paths.get(execution_id)
        if path is None:
            record = s.executions.get(execution_id)
            path = record.owned_cgroup_path if record is not None else None
            if path is not None:
                s._owned_cgroup_paths[execution_id] = path
        if path is None:
            return True
        for attempt in range(10):
            removed = _cleanup_owned_cgroup(path)
            if removed or not Path(path).exists():
                break
            if attempt < 9:
                # Keep retries cooperative: cancelling the delayed GC now
                # guarantees that no worker thread can delete the cgroup while
                # a completion is taking its final snapshot.
                await asyncio.sleep(_OWNED_CGROUP_CLEANUP_RETRY_SECONDS)
        if Path(path).exists():
            return False
        s._owned_cgroup_paths.pop(execution_id, None)
        record = s.executions.get(execution_id)
        if record is not None and record.owned_cgroup_path == path:
            record.owned_cgroup_path = None
        return True

    async def cleanup_owned_cgroup_after_grace(
        s: AppState,
        execution_id: str,
    ) -> None:
        """Eventually collect a cgroup when the completion hook is lost."""

        try:
            while True:
                await asyncio.sleep(_OWNED_CGROUP_CLEANUP_GRACE_SECONDS)
                finalizer = s._ebpf_finalize_tasks.get(execution_id)
                if s.predictor.execution_active(execution_id) or (
                    finalizer is not None and not finalizer.done()
                ):
                    # /exited schedules the eBPF fallback and cgroup GC at
                    # nearly the same time. Do not remove the collector's
                    # scope while it is still producing its final artifact.
                    continue
                if await cleanup_owned_cgroup(s, execution_id):
                    return
        finally:
            current = s._owned_cgroup_cleanup_tasks.get(execution_id)
            if current is asyncio.current_task():
                s._owned_cgroup_cleanup_tasks.pop(execution_id, None)

    def schedule_owned_cgroup_cleanup(s: AppState, execution_id: str) -> None:
        record = s.executions.get(execution_id)
        if record is not None and record.owned_cgroup_path is not None:
            s._owned_cgroup_paths.setdefault(
                execution_id,
                record.owned_cgroup_path,
            )
        if execution_id not in s._owned_cgroup_paths:
            return
        cancel_owned_cgroup_cleanup(s, execution_id)
        s._owned_cgroup_cleanup_tasks[execution_id] = asyncio.create_task(
            cleanup_owned_cgroup_after_grace(s, execution_id)
        )

    def record_ebpf_telemetry(
        s: AppState,
        execution_id: str,
        telemetry: object,
    ) -> None:
        if s.trace_writer is not None:
            s.trace_writer.record_tool_resource_telemetry(execution_id, telemetry)

    def ebpf_needs_finalization(s: AppState, execution_id: str) -> bool:
        # Summary status alone is ambiguous: ``unavailable`` can describe
        # either an active collector or an already finalized failure.  The
        # predictor's active-run registry is the exactly-once authority.
        return s.predictor.execution_active(execution_id)

    async def finalize_ebpf_after_grace(
        s: AppState,
        execution_id: str,
        exit_code: int | None,
        signal: int | None,
    ) -> None:
        """Finalize if the plugin never supplies its authoritative tool result."""

        try:
            await asyncio.sleep(_EBPF_COMPLETION_GRACE_SECONDS)
            telemetry = s.predictor.finish_execution(
                execution_id=execution_id,
                exit_code=exit_code,
                signal=signal,
            )
            record_ebpf_telemetry(s, execution_id, telemetry)
        finally:
            current = s._ebpf_finalize_tasks.get(execution_id)
            if current is asyncio.current_task():
                s._ebpf_finalize_tasks.pop(execution_id, None)

    def schedule_ebpf_fallback(
        s: AppState,
        execution_id: str,
        exit_code: int | None,
        signal: int | None,
    ) -> None:
        cancel_ebpf_fallback(s, execution_id)
        s._ebpf_finalize_tasks[execution_id] = asyncio.create_task(
            finalize_ebpf_after_grace(s, execution_id, exit_code, signal)
        )

    def finish_ebpf_from_completion(
        s: AppState,
        event: ToolCompletedEvent,
        *,
        record: object | None,
        incomplete_reason: str | None = None,
    ) -> None:
        execution_id = event.execution_id
        if execution_id is None:
            return
        cancel_ebpf_fallback(s, execution_id)
        exit_code = getattr(record, "exit_code", None)
        signal = getattr(record, "signal", None)
        if (
            incomplete_reason is None
            and exit_code is None
            and signal is None
            and event.succeeded
        ):
            exit_code = 0
        finish_kwargs: dict[str, Any] = {
            "execution_id": execution_id,
            "exit_code": None if incomplete_reason is not None else exit_code,
            "signal": None if incomplete_reason is not None else signal,
            "raw_result": event.raw_result,
            "succeeded": None if incomplete_reason is not None else event.succeeded,
        }
        if incomplete_reason is not None:
            finish_kwargs["incomplete_reason"] = incomplete_reason
        telemetry = s.predictor.finish_execution(**finish_kwargs)
        record_ebpf_telemetry(s, execution_id, telemetry)

    async def finalize_ebpf_from_completion(
        s: AppState,
        event: ToolCompletedEvent,
    ) -> None:
        execution_id = event.execution_id
        if execution_id is None or _completion_reports_running(event.raw_result):
            return
        record = s.executions.get(execution_id)
        if record is not None and record.exited:
            finish_ebpf_from_completion(s, event, record=record)
            return
        if not ebpf_needs_finalization(s, execution_id):
            return

        # The launcher normally reports /exited before the wrapped command
        # returns to OpenClaw. Give a delayed retry a short reconciliation
        # window, then close the collector as explicitly incomplete. Awaiting
        # here keeps both cleanup and the final telemetry attached to this
        # completion before the host runner can stop the sidecar.
        await asyncio.sleep(_EBPF_ORPHAN_GRACE_SECONDS)
        record = s.executions.get(execution_id)
        if record is not None and record.exited:
            finish_ebpf_from_completion(s, event, record=record)
        elif ebpf_needs_finalization(s, execution_id):
            finish_ebpf_from_completion(
                s,
                event,
                record=record,
                incomplete_reason="launcher_exit_missing",
            )

    def with_sandbox_fallback(request: ToolBeforeRequest, s: AppState) -> ToolBeforeRequest:
        if (
            request.resource_scope is not None
            and not _is_shared_runtime_scope(request.resource_scope)
        ):
            return request
        scope = sandbox_fallback_scope(
            s,
            request.runtime_id,
            request.gateway_id,
        )
        if scope is None:
            return request
        return request.model_copy(update={"resource_scope": scope})

    def completed_with_sandbox_fallback(
        event: ToolCompletedEvent,
        s: AppState,
    ) -> ToolCompletedEvent:
        scope = sandbox_fallback_scope(s, event.runtime_id, event.gateway_id)
        if scope is None:
            return event
        existing = event.resource_scope
        if (
            existing is not None
            and not _is_shared_runtime_scope(existing)
            and (
                _is_authoritative_execution_scope(existing, event.execution_id)
                or _is_verified_host_pid_scope(existing)
                or event.execution_id is None
                or _has_usable_cgroup_scope(existing)
            )
        ):
            return event
        return event.model_copy(update={"resource_scope": scope})

    async def completed_with_execution_scope(
        event: ToolCompletedEvent,
        s: AppState,
    ) -> ToolCompletedEvent:
        if event.execution_id is None:
            return event
        supplied_scope = event.resource_scope
        supplied_scope_is_shared = bool(
            _is_shared_runtime_scope(supplied_scope)
            or _is_shared_sandbox_scope(supplied_scope)
        )
        deadline = time.monotonic() + 0.75
        while True:
            scope = s.executions.scope(event.execution_id)
            # The execution registry is populated by the authenticated launcher
            # lifecycle. Its exact cgroup/PID identity outranks the shared
            # runtime or sandbox scope carried by an OpenClaw completion hook.
            # Delayed cleanup normally keeps the owned cgroup readable through
            # this final snapshot; its recorded identity also survives retries
            # after cleanup.
            if _is_authoritative_execution_scope(scope, event.execution_id):
                return event.model_copy(update={"resource_scope": scope})
            if supplied_scope is not None and not supplied_scope_is_shared:
                return event
            if (
                supplied_scope is None
                and (
                    _has_usable_cgroup_scope(scope)
                    or _is_verified_host_pid_scope(scope)
                    or (
                        sandbox_fallback_scope(
                            s,
                            event.runtime_id,
                            event.gateway_id,
                        )
                        is None
                        and scope is not None
                    )
                )
            ):
                return event.model_copy(update={"resource_scope": scope})
            if time.monotonic() >= deadline:
                return event
            await asyncio.sleep(0.025)

    @app.get("/health/live")
    async def live() -> dict[str, object]:
        return {
            "schema_version": _HEALTH_SCHEMA_VERSION,
            "service": _HEALTH_SERVICE,
            "sidecar_version": _sidecar_version,
            "protocol_versions": _PROTOCOL_VERSIONS,
            "live": True,
        }

    @app.get("/health/ready")
    async def ready() -> dict[str, object]:
        return {
            "schema_version": _HEALTH_SCHEMA_VERSION,
            "service": _HEALTH_SERVICE,
            "sidecar_version": _sidecar_version,
            "protocol_versions": _PROTOCOL_VERSIONS,
            "ready": True,
        }

    @app.get("/v1/status", response_model=StatusResponse)
    async def status(s: AppState = Depends(get_state), _: None = Depends(auth)) -> StatusResponse:
        return StatusResponse(ready=True, policy=s.config.policy, topology=s.topology)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(s: AppState = Depends(get_state)) -> str:
        return s.metrics.render(
            await s.leases.active_count(),
            active_tool_monitors=s.tool_monitor.active_count(),
            active_lease_mcpu=await s.leases.active_mcpu(),
        )

    @app.get("/v1/tools/recent")
    async def recent_tools(
        limit: int = 20,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, object]:
        return {"samples": s._recent_samples[:limit]}

    def store_sandbox_scope(
        s: AppState,
        scope: ResourceScope,
        runtime_id: str | None,
        gateway_id: str | None = None,
    ) -> None:
        if runtime_id is None:
            s._sandbox_scope_override = scope
            if s.docker_exec_observer is not None:
                s.docker_exec_observer.update_container(
                    container_id=scope.container_id,
                )
                s.docker_exec_observer.update_cgroup(
                    cgroup_path=scope.cgroup_path,
                )
        else:
            s._sandbox_scopes_by_owner[(gateway_id, runtime_id)] = scope
            if s.docker_exec_observer is not None:
                s.docker_exec_observer.update_runtime_scope(
                    SimpleNamespace(
                        gateway_id=gateway_id,
                        runtime_id=runtime_id,
                        agent_id=None,
                        session_id=None,
                        run_id=None,
                    ),
                    scope,
                )
                s.docker_exec_observer.update_cgroup(
                    cgroup_path=scope.cgroup_path,
                )

        ebpf_start_failed = False
        failed_execution_id: str | None = None
        for record in s.executions.active():
            if record.request.runtime_id != runtime_id:
                continue
            if (
                gateway_id is not None
                and record.request.gateway_id is not None
                and record.request.gateway_id != gateway_id
            ):
                continue
            started = begin_ebpf_for_record(
                s,
                record.request.execution_id,
                scope.container_id,
            )
            if (
                s.config.tool_resource_ebpf_required
                and record.request.backend == "managed-wrapper"
                and not started
            ):
                ebpf_start_failed = True
                failed_execution_id = record.request.execution_id
            # Repair tool-monitor scopes that were bound from inside the
            # container (launcher's /proc/self/cgroup view) before the
            # host-side sandbox scope was discovered.  The new scope has
            # the correct host cgroup path.
            if (
                record.scope is not None
                and record.scope.cgroup_path
                and record.scope.cgroup_path != scope.cgroup_path
                and not _is_authoritative_execution_scope(
                    record.scope,
                    record.request.execution_id,
                )
            ):
                s.tool_monitor.bind_scope(
                    record.request.tool_call_id,
                    scope,
                    runtime_id=record.request.runtime_id,
                    owner=record.request,
                )
        # Marker-backend executions are registered but never claimed by a
        # launcher.  Start eBPF for them once the sandbox container is
        # discovered so eBPF clause telemetry can capture exec events.
        for record in s.executions.pending_marker():
            if record.request.runtime_id != runtime_id:
                continue
            if (
                gateway_id is not None
                and record.request.gateway_id is not None
                and record.request.gateway_id != gateway_id
            ):
                continue
            begin_ebpf_for_record(
                s,
                record.request.execution_id,
                scope.container_id,
            )
        if ebpf_start_failed:
            raise HTTPException(
                status_code=503,
                detail=ebpf_failure_detail(s, failed_execution_id or "unknown"),
            )

    @app.post("/v1/runtime/sandbox-scope")
    async def update_sandbox_scope(
        scope: ResourceScope,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        store_sandbox_scope(s, scope, None)
        return {"stored": True}

    @app.post("/v1/runtime/{runtime_id}/sandbox-scope")
    async def update_runtime_sandbox_scope(
        runtime_id: str,
        scope: ResourceScope,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        if not runtime_id.strip() or len(runtime_id) > 128:
            raise HTTPException(status_code=422, detail="invalid_runtime_id")
        store_sandbox_scope(s, scope, runtime_id)
        return {"stored": True}

    @app.post(
        "/v1/gateways/{gateway_id}/runtimes/{runtime_id}/sandbox-scope"
    )
    async def update_gateway_runtime_sandbox_scope(
        gateway_id: str,
        runtime_id: str,
        scope: ResourceScope,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        if not gateway_id.strip() or len(gateway_id) > 128:
            raise HTTPException(status_code=422, detail="invalid_gateway_id")
        if not runtime_id.strip() or len(runtime_id) > 128:
            raise HTTPException(status_code=422, detail="invalid_runtime_id")
        store_sandbox_scope(s, scope, runtime_id, gateway_id)
        return {"stored": True}

    @app.delete("/v1/runtime/{runtime_id}/sandbox-scope")
    async def delete_runtime_sandbox_scope(
        runtime_id: str,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        stored = s._sandbox_scopes_by_owner.pop((None, runtime_id), None) is not None
        await s.leases.release_runtime(runtime_id, None)
        if s.trace_writer is not None:
            s.trace_writer.release_runtime(runtime_id)
        s._completed_tool_event_ids.difference_update(
            {
                key
                for key in s._completed_tool_event_ids
                if len(key) >= 2 and key[0] is None and key[1] == runtime_id
            }
        )
        for key in [
            key
            for key in s._tool_decisions_by_event_id
            if len(key) >= 2 and key[0] is None and key[1] == runtime_id
        ]:
            s._tool_decisions_by_event_id.pop(key, None)
        s._model_event_ids.difference_update(
            {
                key
                for key in s._model_event_ids
                if len(key) >= 2 and key[0] is None and key[1] == runtime_id
            }
        )
        return {"stored": stored}

    @app.delete(
        "/v1/gateways/{gateway_id}/runtimes/{runtime_id}/sandbox-scope"
    )
    async def delete_gateway_runtime_sandbox_scope(
        gateway_id: str,
        runtime_id: str,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        stored = (
            s._sandbox_scopes_by_owner.pop((gateway_id, runtime_id), None)
            is not None
        )
        await s.leases.release_runtime(runtime_id, gateway_id)
        if s.trace_writer is not None:
            s.trace_writer.release_runtime(runtime_id, gateway_id)
        s._completed_tool_event_ids.difference_update(
            {
                key
                for key in s._completed_tool_event_ids
                if len(key) >= 2
                and key[1] == runtime_id
                and key[0] == gateway_id
            }
        )
        for key in [
            key
            for key in s._tool_decisions_by_event_id
            if len(key) >= 2
            and key[1] == runtime_id
            and key[0] == gateway_id
        ]:
            s._tool_decisions_by_event_id.pop(key, None)
        s._model_event_ids.difference_update(
            {
                key
                for key in s._model_event_ids
                if len(key) >= 2
                and key[1] == runtime_id
                and key[0] == gateway_id
            }
        )
        return {"stored": stored}

    async def drain_runtime_state(
        s: AppState,
        runtime_id: str,
        gateway_id: str | None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        timeout_seconds = min(max(timeout_seconds, 0.0), 60.0)
        deadline = time.monotonic() + timeout_seconds
        while True:
            records = s.executions.for_runtime(runtime_id, gateway_id)
            record_ids = {record.request.execution_id for record in records}
            active_execution_ids = [
                record.request.execution_id
                for record in records
                if (record.claimed and not record.exited)
                or s.predictor.execution_active(record.request.execution_id)
            ]
            active_by_owner = getattr(s.predictor, "active_execution_ids", None)
            if callable(active_by_owner):
                active_execution_ids = sorted(
                    set(active_execution_ids).union(
                        active_by_owner(runtime_id, gateway_id)
                    )
                )
            pending_finalizers = [
                execution_id
                for execution_id in record_ids
                if execution_id in s._ebpf_finalize_tasks
                and not s._ebpf_finalize_tasks[execution_id].done()
            ]
            active_requests = sum(
                count
                for (owner_gateway, owner_runtime), count
                in s._runtime_activity.items()
                if owner_runtime == runtime_id
                and (
                    gateway_id is None
                    or owner_gateway is None
                    or owner_gateway == gateway_id
                )
            )
            if s.trace_writer is not None:
                active_requests += s.trace_writer.active_runtime_operations(
                    runtime_id
                )
            if (
                not active_execution_ids
                and not pending_finalizers
                and active_requests == 0
            ):
                flush_shared_kb = getattr(s.predictor, "flush_kb_updates", None)
                if callable(flush_shared_kb):
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        await asyncio.to_thread(flush_shared_kb, remaining)
                    except Exception:
                        return {
                            "drained": False,
                            "gateway_id": gateway_id,
                            "runtime_id": runtime_id,
                            "active_executions": 0,
                            "pending_finalizers": 0,
                            "kb_flushed": False,
                        }
                return {
                    "drained": True,
                    "gateway_id": gateway_id,
                    "runtime_id": runtime_id,
                    "active_executions": 0,
                }
            if time.monotonic() >= deadline:
                return {
                    "drained": False,
                    "gateway_id": gateway_id,
                    "runtime_id": runtime_id,
                    "active_executions": len(active_execution_ids),
                    "pending_finalizers": len(pending_finalizers),
                    "active_requests": active_requests,
                    "kb_flushed": False,
                }
            await asyncio.sleep(0.025)

    @app.post("/v1/runtime/{runtime_id}/drain")
    async def drain_runtime(
        runtime_id: str,
        timeout_seconds: float = 15.0,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, object]:
        return await drain_runtime_state(s, runtime_id, None, timeout_seconds)

    @app.post("/v1/gateways/{gateway_id}/runtimes/{runtime_id}/drain")
    async def drain_gateway_runtime(
        gateway_id: str,
        runtime_id: str,
        timeout_seconds: float = 15.0,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, object]:
        return await drain_runtime_state(
            s,
            runtime_id,
            gateway_id,
            timeout_seconds,
        )

    @app.get("/v1/models")
    @app.get("/models")
    async def llm_proxy_models(
        request: Request,
        s: AppState = Depends(get_state),
    ):
        return await proxy_models(request, s.config)

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def llm_proxy_chat_completions(
        request: Request,
        s: AppState = Depends(get_state),
    ):
        return await proxy_chat_completions(request, s.config, s.trace_writer)

    def begin_runtime_activity(value: object) -> tuple[str | None, str | None]:
        key = (
            getattr(value, "gateway_id", None),
            getattr(value, "runtime_id", None),
        )
        app_state._runtime_activity[key] = app_state._runtime_activity.get(key, 0) + 1
        return key

    def end_runtime_activity(key: tuple[str | None, str | None]) -> None:
        remaining = app_state._runtime_activity.get(key, 0) - 1
        if remaining > 0:
            app_state._runtime_activity[key] = remaining
        else:
            app_state._runtime_activity.pop(key, None)

    async def calculate_tool_decision(
        request: ToolBeforeRequest,
        s: AppState,
        decision_key: tuple[str | None, ...],
    ) -> ToolDecision:
        activity_key = begin_runtime_activity(request)
        try:
            start = time.monotonic()
            s.metrics.inc("scheduler_tool_requests_total")
            if s.trace_writer is not None:
                s.trace_writer.record_tool_started(request)
            s.predictor.record_tool_started(request)
            ambient_snapshot = s.tool_monitor.sampler.snapshot(
                request.resource_scope
            )
            ambient_before_mb = _ambient_before_mb(
                request,
                ambient_snapshot.rss_bytes,
            )
            prediction = await asyncio.to_thread(
                s.predictor.predict,
                request,
                ambient_before_mb=ambient_before_mb,
            )
            if s.trace_writer is not None:
                s.trace_writer.record_tool_prediction(request, prediction)
            decision = await s.policy.decide(
                request,
                SchedulingContext(
                    prediction=prediction,
                    placement=PlacementAdvice(),
                ),
            )
            s._tool_decisions_by_event_id[decision_key] = (
                request.params_digest,
                request.tool_name,
                decision,
            )
            while len(s._tool_decisions_by_event_id) > 10_000:
                oldest = next(iter(s._tool_decisions_by_event_id))
                s._tool_decisions_by_event_id.pop(oldest, None)
            if decision.action == "allow":
                s.tool_monitor.begin(request, prediction.resource_class)
                if s.docker_exec_observer is not None:
                    s.docker_exec_observer.begin_tool(request)
            s.metrics.inc("scheduler_tool_decisions_total")
            s.metrics.decision_latencies.append(time.monotonic() - start)
            return decision
        finally:
            end_runtime_activity(activity_key)

    @app.post("/v1/decisions/tool")
    async def decide_tool(
        request: ToolBeforeRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ):
        request = with_sandbox_fallback(request, s)
        decision_key = correlation_key(request, request.event_id)
        cached_decision = s._tool_decisions_by_event_id.get(decision_key)
        if cached_decision is not None:
            cached_digest, cached_tool, decision = cached_decision
            if (
                cached_digest != request.params_digest
                or cached_tool != request.tool_name
            ):
                raise HTTPException(
                    status_code=409,
                    detail="tool_event_id_payload_mismatch",
                )
            return decision
        task = s._decision_tasks.get(decision_key)
        if task is None:
            task = asyncio.create_task(
                calculate_tool_decision(request, s, decision_key)
            )
            s._decision_tasks[decision_key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and s._decision_tasks.get(decision_key) is task:
                s._decision_tasks.pop(decision_key, None)

    @app.post("/v1/events/tool-completed")
    async def complete_tool(
        event: ToolCompletedEvent,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        if event.execution_id is not None:
            execution_record = s.executions.get(event.execution_id)
            if (
                execution_record is not None
                and not owners_compatible(execution_record.request, event)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "execution_runtime_mismatch"
                        if execution_record.request.runtime_id
                        != event.runtime_id
                        else "execution_owner_mismatch"
                    ),
                )
        # Dedup before touching the deferred eBPF finalizer. A duplicate
        # completion must not cancel the only remaining fallback task.
        completion_key = correlation_key(event, event.event_id)
        if completion_key in s._completed_tool_event_ids:
            return {"stored": False}
        s._completed_tool_event_ids.add(completion_key)
        if len(s._completed_tool_event_ids) > 10_000:
            oldest_unknown = next(
                key
                for key in s._completed_tool_event_ids
                if key != completion_key
            )
            s._completed_tool_event_ids.discard(oldest_unknown)
        activity_key = begin_runtime_activity(event)
        if event.execution_id is not None:
            # The delayed exit fallback must not race the final resource
            # snapshot or trace flush performed by this completion.
            cancel_owned_cgroup_cleanup(s, event.execution_id)
        try:
            # Finalize before the scope lookup's async wait.  The plugin may
            # time out its completion POST and issue a telemetry GET; keeping
            # this synchronous section first prevents that GET from observing
            # (or formerly finalizing) a run without the authoritative result.
            # OpenClaw emits a normal-looking tool completion when exec merely
            # yields to its background process manager. That result is not the
            # workload's causal end. A genuinely terminal completion without
            # the launcher's exit is instead closed after a short grace period
            # as explicitly incomplete, never as an inferred success.
            await finalize_ebpf_from_completion(s, event)

            event = await completed_with_execution_scope(event, s)
            inferred_scope = (
                s.docker_exec_observer.infer_scope(event)
                if s.docker_exec_observer is not None
                else None
            )
            if inferred_scope is not None:
                s.tool_monitor.bind_scope(
                    event.tool_call_id,
                    inferred_scope,
                    runtime_id=event.runtime_id,
                    owner=event,
                )
                event = event.model_copy(update={"resource_scope": inferred_scope})
            else:
                event = completed_with_sandbox_fallback(event, s)
            await s.leases.release(
                event.lease_id,
                owner=owner_key(event),
            )
            sample = s.tool_monitor.complete(event)
            if sample is not None:
                s.predictor.observe_completion(event, sample)
                s.metrics.observe_tool_runtime(sample)
                s._recent_samples.insert(0, _sample_summary(sample))
                if len(s._recent_samples) > s._max_recent_samples:
                    s._recent_samples.pop()
                if s.trace_writer is not None:
                    s.trace_writer.record_tool(event, sample)
                    # Make the {"stored": True} acknowledgement durable: the
                    # trace writer persists asynchronously on a dedicated
                    # thread, so drain its queue before responding.
                    await asyncio.to_thread(s.trace_writer.flush)
            if event.execution_id is not None:
                s.executions.mark_completed(event.execution_id)
                if not await cleanup_owned_cgroup(s, event.execution_id):
                    schedule_owned_cgroup_cleanup(s, event.execution_id)
            s.metrics.inc("scheduler_tool_completions_total")
            s.metrics.tool_durations.append(event.duration_ms / 1000)
            return {"stored": True}
        except BaseException:
            # Permit a genuine retry when a fallible downstream step failed.
            # If finalization itself did not acquire the run, restore its
            # launcher-exit fallback as well.
            s._completed_tool_event_ids.discard(completion_key)
            if event.execution_id is not None:
                record = s.executions.get(event.execution_id)
                if record is not None and record.exited:
                    schedule_owned_cgroup_cleanup(s, event.execution_id)
                if (
                    record is not None
                    and record.exited
                    and ebpf_needs_finalization(s, event.execution_id)
                ):
                    schedule_ebpf_fallback(
                        s,
                        event.execution_id,
                        record.exit_code,
                        record.signal,
                    )
            raise
        finally:
            end_runtime_activity(activity_key)

    @app.post("/v1/events/model")
    async def model_event(
        event: ModelEvent,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        model_key = correlation_key(event, event.event_id)
        if model_key in s._model_event_ids:
            return {"stored": False}
        s._model_event_ids.add(model_key)
        activity_key = begin_runtime_activity(event)
        while len(s._model_event_ids) > 20_000:
            oldest = next(iter(s._model_event_ids))
            s._model_event_ids.discard(oldest)
        try:
            if s.trace_writer is not None:
                s.trace_writer.record_model(event)
                # Drain the asynchronous writer so the stored acknowledgement
                # is durable before the plugin proceeds.
                await asyncio.to_thread(s.trace_writer.flush)
            return {"stored": True}
        except BaseException:
            s._model_event_ids.discard(model_key)
            raise
        finally:
            end_runtime_activity(activity_key)

    @app.post("/v2/executions", response_model=ExecutionRegistrationResponse)
    async def register_execution(
        request: ExecutionRegistrationRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionRegistrationResponse:
        previous = s.executions.get(request.execution_id)
        if previous is not None:
            # Validate an idempotent retry before touching its existing lease.
            response = s.executions.register(request)
            if request.lease_id is not None and not await s.leases.bind_execution(
                request.lease_id,
                request.execution_id,
                owner=owner_key(request),
            ):
                raise HTTPException(
                    status_code=409,
                    detail="invalid_or_expired_execution_lease",
                )
        else:
            if request.lease_id is not None and not await s.leases.bind_execution(
                request.lease_id,
                request.execution_id,
                owner=owner_key(request),
            ):
                raise HTTPException(
                    status_code=409,
                    detail="invalid_or_expired_execution_lease",
                )
            try:
                response = s.executions.register(request)
            except BaseException:
                await s.leases.release(
                    request.lease_id,
                    owner=owner_key(request),
                )
                raise
        # For marker-backend executions, start eBPF eBPF telemetry
        # immediately if the sandbox container is already known.  (For
        # managed-wrapper, telemetry starts at claim/started time.)
        if (
            getattr(request, "backend", None) == "marker"
        ):
            container_id = sandbox_container_id(
                s,
                request.runtime_id,
                request.gateway_id,
            )
            if container_id:
                begin_ebpf_for_record(s, request.execution_id, container_id)
        return response

    @app.get("/v2/executions/{execution_id}/scope", response_model=ExecutionScopeResponse)
    async def execution_scope(
        execution_id: str,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionScopeResponse:
        return ExecutionScopeResponse(execution_scope=s.executions.scope(execution_id))

    @app.get("/v2/executions/{execution_id}/telemetry")
    async def execution_telemetry(
        execution_id: str,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, object | None]:
        telemetry = s.predictor.execution_telemetry(execution_id)
        if telemetry is None:
            return {"tool_resource": None}
        if hasattr(telemetry, "model_dump"):
            return {"tool_resource": telemetry.model_dump(mode="json")}
        if isinstance(telemetry, dict):
            return {"tool_resource": telemetry}
        return {"tool_resource": None}

    @app.post("/v2/executions/claim", response_model=ExecutionClaimResponse)
    async def claim_execution(
        request: ExecutionClaimRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionClaimResponse:
        response = s.executions.claim(request)
        record = s.executions.get(request.execution_id)
        if record is not None:
            container_id = sandbox_container_id(
                s,
                record.request.runtime_id,
                record.request.gateway_id,
            )
            # In host-openclaw-sandbox mode the sandbox container is often
            # discovered just after the launcher claims the execution.  Do not
            # consume the eBPF observer opportunity with a permanent
            # container_id_unavailable record; /started will retry once the
            # launcher or sandbox-scope discovery supplies the container id.
            #
            # When ebpf is required but the container id is not yet
            # available (host-openclaw race), defer rather than failing:
            # /v1/runtime/sandbox-scope will retry begin_ebpf_for_record
            # for all active executions once the sandbox container is
            # discovered.
            fallback_scope = sandbox_fallback_scope(
                s,
                record.request.runtime_id,
                record.request.gateway_id,
            )
            if (
                container_id
                and (
                    not _defer_ebpf_until_started(record)
                    or (
                        s.config.execution_cgroup_root is None
                        and _has_usable_cgroup_scope(fallback_scope)
                    )
                )
            ):
                started = begin_ebpf_for_record(s, request.execution_id, container_id)
                if (
                    s.config.tool_resource_ebpf_required
                    and record.request.backend == "managed-wrapper"
                    and not started
                ):
                    raise HTTPException(
                        status_code=503,
                        detail=ebpf_failure_detail(s, request.execution_id),
                    )
            # else: container_id not yet known — ebpf start is deferred to
            # execution_started / sandbox-scope discovery (see above).
        return response

    @app.post(
        "/v2/executions/{execution_id}/started",
        response_model=ExecutionUpdateResponse,
        response_model_exclude_none=True,
    )
    async def execution_started(
        execution_id: str,
        request: ExecutionStartedRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionUpdateResponse:
        response = s.executions.started(execution_id, request)
        record = s.executions.get(execution_id)
        runtime_id = record.request.runtime_id if record is not None else None
        gateway_id = record.request.gateway_id if record is not None else None
        fallback_scope = sandbox_fallback_scope(s, runtime_id, gateway_id)
        container_id = request.container_id or sandbox_container_id(
            s,
            runtime_id,
            gateway_id,
        )
        trusted_root_pid = (
            _resolve_host_pid(
                request.child_pid,
                pid_namespace_inode=request.pid_namespace_inode,
                starttime_ticks=request.process_starttime_ticks,
            )
            if request.pid_namespace_inode is not None
            and request.process_starttime_ticks is not None
            else None
        )
        if record is not None and trusted_root_pid is not None:
            s.executions.bind_trusted_root(execution_id, trusted_root_pid)
        host_cgroup_gate_failed = False
        if record is not None and request.host_cgroup_gate:
            cgroup_diagnostics: list[str] = []
            host_scope = _prepare_host_execution_cgroup(
                execution_id,
                request,
                fallback_scope,
                s.config.execution_cgroup_root,
                diagnostics=cgroup_diagnostics,
            )
            if host_scope is not None:
                s.executions.update_scope(
                    execution_id,
                    host_scope,
                    owned_cgroup_path=host_scope.cgroup_path,
                )
                if host_scope.cgroup_path is not None:
                    s._owned_cgroup_paths[execution_id] = host_scope.cgroup_path
                record = s.executions.get(execution_id)
                response = ExecutionUpdateResponse(
                    stored=True,
                    cgroup_path=host_scope.cgroup_path,
                )
            else:
                # Some systemd/openEuler hosts do not delegate creation of a
                # child cgroup even to this privileged service. Keep eBPF
                # mandatory, but fall back to the authenticated root PID and
                # its sidecar-derived current cgroup. Telemetry then isolates
                # only that PID's fork/exec descendants rather than treating
                # the shared session cgroup as an identity boundary.
                # Surface exactly which candidate root/step failed so the
                # operator can distinguish "no delegation" from a code issue.
                if cgroup_diagnostics:
                    _write_trace_dir_diag(
                        s,
                        "host_cgroup_provision_last_error.txt",
                        cgroup_diagnostics,
                    )
                if request.cgroup_required:
                    host_cgroup_gate_failed = True
                    record.scope = None
                    _log_execution_started_decision(
                        execution_id,
                        request,
                        trusted_root_pid,
                        record.scope,
                        host_cgroup_gate_failed,
                        record.request.backend,
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="exclusive_execution_cgroup_unavailable",
                    )
                host_scope = (
                    _verified_host_execution_scope(
                        execution_id,
                        request,
                        trusted_root_pid,
                    )
                    if trusted_root_pid is not None
                    else None
                )
                if host_scope is not None:
                    s.executions.update_scope(execution_id, host_scope)
                    record = s.executions.get(execution_id)
                    response = ExecutionUpdateResponse(
                        stored=True,
                        cgroup_path=host_scope.cgroup_path,
                    )
                else:
                    host_cgroup_gate_failed = True
                    record.scope = None
        elif (
            record is not None
            and container_id is None
            and record.request.backend == "managed-wrapper"
        ):
            host_scope = (
                _verified_host_execution_scope(
                    execution_id,
                    request,
                    trusted_root_pid,
                )
                if trusted_root_pid is not None
                else None
            )
            if host_scope is not None:
                s.executions.update_scope(execution_id, host_scope)
                record = s.executions.get(execution_id)
                response = ExecutionUpdateResponse(
                    stored=True,
                    cgroup_path=host_scope.cgroup_path,
                )
            else:
                # A direct-host claim is usable only when the sidecar itself
                # resolves both PID identity and cgroup membership. The
                # launcher-supplied path is never an attribution authority.
                record.scope = None
        # Host-backed fallback: when no per-execution cgroup could be created
        # on the host (read-only-cgroupfs sandbox, or a launcher-side container
        # cgroup that is not host-valid) but the launcher PID was resolved to a
        # verified host PID, first derive the process's ACTUAL host cgroup from
        # /proc/<host_pid>/cgroup (cgroup-backed), and only if that is not
        # usable fall back to per-PID process-tree attribution.
        if (
            record is not None
            and trusted_root_pid is not None
            and (record.scope is None or not _has_usable_cgroup_scope(record.scope))
        ):
            host_scope = _verified_host_execution_scope(
                execution_id,
                request,
                trusted_root_pid,
            )
            if host_scope is None:
                host_scope = _verified_host_pid_scope(
                    execution_id,
                    trusted_root_pid,
                    request.process_starttime_ticks,
                )
            s.executions.update_scope(execution_id, host_scope)
            record = s.executions.get(execution_id)
        if record is not None:
            _log_execution_started_decision(
                execution_id,
                request,
                trusted_root_pid,
                record.scope,
                host_cgroup_gate_failed,
                record.request.backend,
            )
        if record is not None and record.scope is not None:
            monitor_scope = record.scope
            if (
                fallback_scope is not None
                and not _has_usable_cgroup_scope(monitor_scope)
                and not _is_verified_host_pid_scope(monitor_scope)
            ):
                # In host-openclaw-sandbox mode launcher PIDs belong to the
                # container PID namespace.  Sampling the same numeric PID on
                # the host can silently attribute an unrelated host process.
                # Keep the already discovered host-side sandbox cgroup unless
                # the launcher supplies a real cgroup-v2 child scope.
                monitor_scope = fallback_scope
            s.tool_monitor.bind_scope(
                record.request.tool_call_id,
                monitor_scope,
                runtime_id=record.request.runtime_id,
                owner=record.request,
            )
        if record is not None:
            # The launcher runs inside the sandbox container.  Its
            # cgroup_path comes from the container's cgroup namespace
            # and may be the host root (/sys/fs/cgroup) when cgroupfs
            # is read-only inside the container.  Only pass through
            # paths that are actual sub-cgroups, not the root fallback.
            host_scope_ready = bool(
                container_id is not None
                or (
                    trusted_root_pid is not None
                    and _has_usable_cgroup_scope(record.scope)
                )
            )
            started = (
                begin_ebpf_for_record(
                    s,
                    execution_id,
                    container_id,
                    cgroup_path=_trusted_cgroup_path(
                        record.scope.cgroup_path
                        if record.scope is not None
                        else None
                    ),
                    trusted_root_pid=trusted_root_pid,
                )
                if host_scope_ready and not host_cgroup_gate_failed
                else False
            )
            if (
                s.config.tool_resource_ebpf_required
                and record.request.backend == "managed-wrapper"
                and not started
            ):
                raise HTTPException(
                    status_code=503,
                    detail=ebpf_failure_detail(s, execution_id),
                )
        return response

    @app.post(
        "/v2/executions/{execution_id}/exited",
        response_model=ExecutionUpdateResponse,
        response_model_exclude_none=True,
    )
    async def execution_exited(
        execution_id: str,
        request: ExecutionExitedRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionUpdateResponse:
        response = s.executions.exited(execution_id, request)
        await s.leases.release_execution(execution_id)
        # The launcher knows process status first, but only OpenClaw's
        # subsequent completion event carries bounded stdout/stderr. Keep the
        # collector open briefly so a masked lookup failure such as
        # ``missing | tail`` can be classified exactly. The timeout preserves
        # finalization if the completion hook is lost.
        if ebpf_needs_finalization(s, execution_id):
            schedule_ebpf_fallback(
                s,
                execution_id,
                request.exit_code,
                request.signal,
            )
        # The launcher waits for this response before OpenClaw can emit the
        # completion hook. Keep the empty cgroup alive for that hook's final
        # cpu/memory/io snapshot, with delayed collection if the hook is lost.
        schedule_owned_cgroup_cleanup(s, execution_id)
        return response

    return app
