from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent_scheduler.api.dependencies import AppState, build_state
from agent_scheduler.contracts.models import (
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
)
from agent_scheduler.llm_proxy import proxy_chat_completions, proxy_models
from agent_scheduler.monitoring.tool_runtime import ToolRuntimeSample
from agent_scheduler.policies.base import SchedulingContext
from agent_scheduler.security.auth import verify_bearer


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


def _profiling_enabled(profiling: object, key: str, default: bool = False) -> bool:
    if not isinstance(profiling, dict):
        return default
    value = profiling.get(key)
    return value if isinstance(value, bool) else default


def _defer_stage2_until_started(record: object) -> bool:
    request = getattr(record, "request", None)
    return bool(
        request is not None
        and getattr(request, "backend", None) == "managed-wrapper"
        and _profiling_enabled(getattr(request, "profiling", None), "enable_cgroup")
    )


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


def _prepare_host_execution_cgroup(
    execution_id: str,
    request: ExecutionStartedRequest,
    fallback_scope: ResourceScope | None,
    configured_root: str | None,
) -> ResourceScope | None:
    root = configured_root or (
        f"{fallback_scope.cgroup_path.rstrip('/')}/claw-executions"
        if fallback_scope is not None and fallback_scope.cgroup_path
        else None
    )
    if root is None:
        return None
    host_pid = _resolve_host_pid(
        request.child_pid,
        pid_namespace_inode=request.pid_namespace_inode,
        starttime_ticks=request.process_starttime_ticks,
    )
    if host_pid is None:
        return None
    cgroup_path = Path(root) / _safe_cgroup_name(execution_id)
    try:
        cgroup_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        (cgroup_path / "cgroup.procs").write_text(str(host_pid), encoding="utf-8")
    except OSError:
        return None
    if not _host_pid_in_cgroup(host_pid, cgroup_path):
        _cleanup_owned_cgroup(str(cgroup_path))
        return None
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
        source="claw-sidecar-host-cgroup",
        attribution_source="exclusive-execution-cgroup",
    )


def _host_pid_in_cgroup(host_pid: int, cgroup_path: Path) -> bool:
    try:
        procs = (cgroup_path / "cgroup.procs").read_text(encoding="utf-8").split()
    except OSError:
        return False
    return str(host_pid) in procs


def _cleanup_owned_cgroup(path: str | None) -> None:
    if not path:
        return
    cgroup = Path(path)
    for _attempt in range(10):
        try:
            if (cgroup / "cgroup.procs").read_text(encoding="utf-8").strip():
                return
        except OSError:
            return
        try:
            cgroup.rmdir()
            return
        except OSError:
            time.sleep(0.02)


def create_app(state: AppState | None = None) -> FastAPI:
    app_state = state or build_state()
    app = FastAPI(title="OpenClaw Agent Scheduler Sidecar", version="0.1.0")
    app.state.scheduler = app_state

    def get_state() -> AppState:
        return app.state.scheduler

    def auth(s: AppState = Depends(get_state)) -> None:
        verify_bearer(s.config.auth_token)

    def sandbox_fallback_scope(s: AppState) -> ResourceScope | None:
        if s._sandbox_scope_override is not None:
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

    def sandbox_container_id(s: AppState) -> str | None:
        if s.config.sandbox_container_id:
            return s.config.sandbox_container_id
        if s._sandbox_scope_override is not None:
            return s._sandbox_scope_override.container_id
        return None

    def begin_stage2_for_record(
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
        stage2_kwargs: dict[str, Any] = {
            "execution_id": execution_id,
            "tool_call_id": record.request.tool_call_id,
            "command": record.request.command,
            "container_id": container_id,
            "repo": s.config.tool_resource_repo,
        }
        if root_pid is not None:
            stage2_kwargs["trusted_root_pid"] = root_pid
        if cgroup_path is None:
            return s.predictor.begin_execution(**stage2_kwargs)
        stage2_kwargs["cgroup_path"] = cgroup_path
        return s.predictor.begin_execution(**stage2_kwargs)

    def with_sandbox_fallback(request: ToolBeforeRequest, s: AppState) -> ToolBeforeRequest:
        if (
            request.resource_scope is not None
            and not _is_shared_runtime_scope(request.resource_scope)
        ):
            return request
        scope = sandbox_fallback_scope(s)
        if scope is None:
            return request
        return request.model_copy(update={"resource_scope": scope})

    def completed_with_sandbox_fallback(
        event: ToolCompletedEvent,
        s: AppState,
    ) -> ToolCompletedEvent:
        scope = sandbox_fallback_scope(s)
        if scope is None:
            return event
        existing = event.resource_scope
        if (
            existing is not None
            and not _is_shared_runtime_scope(existing)
            and (
                event.execution_id is None
                or _has_usable_cgroup_scope(existing)
            )
        ):
            return event
        return event.model_copy(update={"resource_scope": scope})

    async def completed_with_execution_scope(
        event: ToolCompletedEvent,
        s: AppState,
    ) -> ToolCompletedEvent:
        if event.resource_scope is not None or event.execution_id is None:
            return event
        deadline = time.monotonic() + 0.75
        while True:
            scope = s.executions.scope(event.execution_id)
            if (
                _has_usable_cgroup_scope(scope)
                or (
                    sandbox_fallback_scope(s) is None
                    and scope is not None
                )
            ):
                return event.model_copy(update={"resource_scope": scope})
            if time.monotonic() >= deadline:
                return event
            await asyncio.sleep(0.025)

    @app.get("/health/live")
    async def live() -> dict[str, bool]:
        return {"live": True}

    @app.get("/health/ready")
    async def ready() -> dict[str, bool]:
        return {"ready": True}

    @app.get("/v1/status", response_model=StatusResponse)
    async def status(s: AppState = Depends(get_state), _: None = Depends(auth)) -> StatusResponse:
        return StatusResponse(ready=True, policy=s.config.policy, topology=s.topology)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(s: AppState = Depends(get_state)) -> str:
        return s.metrics.render(
            await s.leases.active_count(),
            active_tool_monitors=s.tool_monitor.active_count(),
        )

    @app.get("/v1/tools/recent")
    async def recent_tools(
        limit: int = 20,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, object]:
        return {"samples": s._recent_samples[:limit]}

    @app.post("/v1/runtime/sandbox-scope")
    async def update_sandbox_scope(
        scope: ResourceScope,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        s._sandbox_scope_override = scope
        if s.docker_exec_observer is not None:
            s.docker_exec_observer.update_container(
                container_id=scope.container_id,
            )
        stage2_start_failed = False
        for record in s.executions.active():
            started = begin_stage2_for_record(
                s,
                record.request.execution_id,
                scope.container_id,
            )
            if (
                s.config.tool_resource_stage2_required
                and record.request.backend == "managed-wrapper"
                and not started
            ):
                stage2_start_failed = True
            # Repair tool-monitor scopes that were bound from inside the
            # container (launcher's /proc/self/cgroup view) before the
            # host-side sandbox scope was discovered.  The new scope has
            # the correct host cgroup path.
            if (
                record.scope is not None
                and record.scope.cgroup_path
                and record.scope.cgroup_path != scope.cgroup_path
            ):
                s.tool_monitor.bind_scope(
                    record.request.tool_call_id, scope
                )
        # Marker-backend executions are registered but never claimed by a
        # launcher.  Start Stage-2 for them once the sandbox container is
        # discovered so eBPF clause telemetry can capture exec events.
        for record in s.executions.pending_marker():
            begin_stage2_for_record(
                s,
                record.request.execution_id,
                scope.container_id,
            )
        if stage2_start_failed:
            raise HTTPException(
                status_code=503,
                detail="tool_resource_stage2_start_failed",
            )
        return {"stored": True}

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

    @app.post("/v1/decisions/tool")
    async def decide_tool(
        request: ToolBeforeRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ):
        original_request = request
        request = with_sandbox_fallback(request, s)
        start = time.monotonic()
        s.metrics.inc("scheduler_tool_requests_total")
        if s.trace_writer is not None:
            s.trace_writer.record_tool_started(request)
        s.predictor.record_tool_started(request)
        ambient_snapshot = s.tool_monitor.sampler.snapshot(request.resource_scope)
        ambient_before_mb = _ambient_before_mb(request, ambient_snapshot.rss_bytes)
        prediction = await s.predictor.predict(
            request,
            ambient_before_mb=ambient_before_mb,
        )
        if s.trace_writer is not None:
            s.trace_writer.record_tool_prediction(request, prediction)
        decision = await s.policy.decide(
            request,
            SchedulingContext(prediction=prediction, placement=PlacementAdvice()),
        )
        if decision.action == "allow":
            s.tool_monitor.begin(request, prediction.resource_class)
            if s.docker_exec_observer is not None:
                s.docker_exec_observer.begin_tool(original_request)
        s.metrics.inc("scheduler_tool_decisions_total")
        s.metrics.decision_latencies.append(time.monotonic() - start)
        return decision

    @app.post("/v1/events/tool-completed")
    async def complete_tool(
        event: ToolCompletedEvent,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        event = await completed_with_execution_scope(event, s)
        inferred_scope = (
            s.docker_exec_observer.infer_scope(event)
            if s.docker_exec_observer is not None
            else None
        )
        if inferred_scope is not None:
            s.tool_monitor.bind_scope(event.tool_call_id, inferred_scope)
            event = event.model_copy(update={"resource_scope": inferred_scope})
        else:
            event = completed_with_sandbox_fallback(event, s)
        # Dedup: reject duplicate tool completions (same event_id)
        if event.event_id in s._completed_tool_event_ids:
            return {"stored": False}
        s._completed_tool_event_ids.add(event.event_id)

        await s.leases.release(event.lease_id)
        sample = s.tool_monitor.complete(event)
        telemetry = None
        if event.execution_id is not None:
            record = s.executions.get(event.execution_id)
            exit_code = record.exit_code if record is not None else None
            signal = record.signal if record is not None else None
            if exit_code is None and signal is None and event.succeeded:
                exit_code = 0
            telemetry = s.predictor.finish_execution(
                execution_id=event.execution_id,
                exit_code=exit_code,
                signal=signal,
            )
            if s.trace_writer is not None:
                s.trace_writer.record_tool_resource_telemetry(
                    event.execution_id,
                    telemetry,
                )
        if sample is not None:
            s.predictor.observe_completion(event, sample)
            s.metrics.observe_tool_runtime(sample)
            s._recent_samples.insert(0, _sample_summary(sample))
            if len(s._recent_samples) > s._max_recent_samples:
                s._recent_samples.pop()
            if s.trace_writer is not None:
                s.trace_writer.record_tool(event, sample)
        s.metrics.inc("scheduler_tool_completions_total")
        s.metrics.tool_durations.append(event.duration_ms / 1000)
        return {"stored": True}

    @app.post("/v1/events/model")
    async def model_event(
        event: ModelEvent,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> dict[str, bool]:
        if s.trace_writer is not None:
            s.trace_writer.record_model(event)
        return {"stored": True}

    @app.post("/v2/executions", response_model=ExecutionRegistrationResponse)
    async def register_execution(
        request: ExecutionRegistrationRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionRegistrationResponse:
        response = s.executions.register(request)
        # For marker-backend executions, start Stage-2 eBPF telemetry
        # immediately if the sandbox container is already known.  (For
        # managed-wrapper, telemetry starts at claim/started time.)
        if (
            getattr(request, "backend", None) == "marker"
        ):
            container_id = sandbox_container_id(s)
            if container_id:
                begin_stage2_for_record(s, request.execution_id, container_id)
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
        container_id = sandbox_container_id(s)
        if record is not None:
            # In host-openclaw-sandbox mode the sandbox container is often
            # discovered just after the launcher claims the execution.  Do not
            # consume the Stage-2 observer opportunity with a permanent
            # container_id_unavailable record; /started will retry once the
            # launcher or sandbox-scope discovery supplies the container id.
            #
            # When stage2 is required but the container id is not yet
            # available (host-sandbox race), defer rather than failing:
            # /v1/runtime/sandbox-scope will retry begin_stage2_for_record
            # for all active executions once the sandbox container is
            # discovered.
            fallback_scope = sandbox_fallback_scope(s)
            if (
                container_id
                and (
                    not _defer_stage2_until_started(record)
                    or (
                        s.config.execution_cgroup_root is None
                        and _has_usable_cgroup_scope(fallback_scope)
                    )
                )
            ):
                started = begin_stage2_for_record(s, request.execution_id, container_id)
                if (
                    s.config.tool_resource_stage2_required
                    and record.request.backend == "managed-wrapper"
                    and not started
                ):
                    raise HTTPException(
                        status_code=503,
                        detail="tool_resource_stage2_start_failed",
                    )
            # else: container_id not yet known — stage2 start is deferred to
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
        fallback_scope = sandbox_fallback_scope(s)
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
        if record is not None and request.host_cgroup_gate:
            host_scope = _prepare_host_execution_cgroup(
                execution_id,
                request,
                fallback_scope,
                s.config.execution_cgroup_root,
            )
            if host_scope is not None:
                s.executions.update_scope(
                    execution_id,
                    host_scope,
                    owned_cgroup_path=host_scope.cgroup_path,
                )
                record = s.executions.get(execution_id)
                response = ExecutionUpdateResponse(
                    stored=True,
                    cgroup_path=host_scope.cgroup_path,
                )
        if record is not None and record.scope is not None:
            monitor_scope = record.scope
            if (
                fallback_scope is not None
                and not _has_usable_cgroup_scope(monitor_scope)
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
            )
        container_id = request.container_id or sandbox_container_id(s)
        if record is not None:
            # The launcher runs inside the sandbox container.  Its
            # cgroup_path comes from the container's cgroup namespace
            # and may be the host root (/sys/fs/cgroup) when cgroupfs
            # is read-only inside the container.  Only pass through
            # paths that are actual sub-cgroups, not the root fallback.
            started = begin_stage2_for_record(
                s, execution_id, container_id,
                cgroup_path=_trusted_cgroup_path(
                    record.scope.cgroup_path if record.scope is not None else request.cgroup_path
                ),
                trusted_root_pid=trusted_root_pid,
            )
            if (
                s.config.tool_resource_stage2_required
                and record.request.backend == "managed-wrapper"
                and not started
            ):
                raise HTTPException(
                    status_code=503,
                    detail="tool_resource_stage2_start_failed",
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
        telemetry = s.predictor.finish_execution(
            execution_id=execution_id,
            exit_code=request.exit_code,
            signal=request.signal,
        )
        record = s.executions.get(execution_id)
        if record is not None:
            _cleanup_owned_cgroup(record.owned_cgroup_path)
        if s.trace_writer is not None:
            s.trace_writer.record_tool_resource_telemetry(execution_id, telemetry)
        return response

    return app
