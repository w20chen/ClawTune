from __future__ import annotations

import asyncio
import time

from fastapi import Depends, FastAPI, Request
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
    if scope is None:
        return False
    cgroup_path = scope.cgroup_path
    if not cgroup_path:
        return True
    normalized = cgroup_path.replace("\\", "/").rstrip("/")
    return normalized not in {"/sys/fs/cgroup", "/sys/fs/cgroup/unified"}


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

    def with_sandbox_fallback(request: ToolBeforeRequest, s: AppState) -> ToolBeforeRequest:
        if request.resource_scope is not None:
            return request
        scope = sandbox_fallback_scope(s)
        if scope is None:
            return request
        return request.model_copy(update={"resource_scope": scope})

    def completed_with_sandbox_fallback(
        event: ToolCompletedEvent,
        s: AppState,
    ) -> ToolCompletedEvent:
        if event.resource_scope is not None:
            return event
        scope = sandbox_fallback_scope(s)
        if scope is None:
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
            if _has_usable_cgroup_scope(scope):
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
            if s.docker_exec_observer is not None:
                s.docker_exec_observer.begin_tool(original_request)
            s.tool_monitor.begin(request, prediction.resource_class)
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
        return s.executions.register(request)

    @app.get("/v2/executions/{execution_id}/scope", response_model=ExecutionScopeResponse)
    async def execution_scope(
        execution_id: str,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionScopeResponse:
        return ExecutionScopeResponse(execution_scope=s.executions.scope(execution_id))

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
            if container_id:
                s.predictor.begin_execution(
                    execution_id=request.execution_id,
                    tool_call_id=record.request.tool_call_id,
                    command=record.request.command,
                    container_id=container_id,
                    repo=s.config.tool_resource_repo,
                )
        return response

    @app.post("/v2/executions/{execution_id}/started", response_model=ExecutionUpdateResponse)
    async def execution_started(
        execution_id: str,
        request: ExecutionStartedRequest,
        s: AppState = Depends(get_state),
        _: None = Depends(auth),
    ) -> ExecutionUpdateResponse:
        response = s.executions.started(execution_id, request)
        record = s.executions.get(execution_id)
        if record is not None and record.scope is not None:
            s.tool_monitor.bind_scope(record.request.tool_call_id, record.scope)
        container_id = request.container_id or sandbox_container_id(s)
        if record is not None:
            s.predictor.begin_execution(
                execution_id=execution_id,
                tool_call_id=record.request.tool_call_id,
                command=record.request.command,
                container_id=container_id,
                repo=s.config.tool_resource_repo,
            )
        return response

    @app.post("/v2/executions/{execution_id}/exited", response_model=ExecutionUpdateResponse)
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
        if s.trace_writer is not None:
            s.trace_writer.record_tool_resource_telemetry(execution_id, telemetry)
        return response

    return app
