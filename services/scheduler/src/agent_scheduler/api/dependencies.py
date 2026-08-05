from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field

from agent_scheduler.admission.leases import LeaseManager
from agent_scheduler.config import SchedulerConfig
from agent_scheduler.contracts.models import ResourceScope, ToolDecision
from agent_scheduler.executions import ExecutionRegistry
from agent_scheduler.monitoring.docker_exec import DockerExecObserver
from agent_scheduler.monitoring.tool_runtime import RealtimeToolMonitor
from agent_scheduler.policies.base import SchedulingPolicy
from agent_scheduler.policies.concurrency import ConcurrencyPolicy
from agent_scheduler.policies.observe import ObserveOnlyPolicy
from agent_scheduler.predictors.tool_resource import ToolResourcePredictor
from agent_scheduler.telemetry.metrics import Metrics
from agent_scheduler.topology.linux import read_topology
from agent_scheduler.trace import AgentTestBenchTraceWriter
from tool_resource.runtime_kb import LatencyBuckets


@dataclass
class AppState:
    config: SchedulerConfig
    predictor: ToolResourcePredictor
    leases: LeaseManager
    policy: SchedulingPolicy
    tool_monitor: RealtimeToolMonitor
    docker_exec_observer: DockerExecObserver | None
    executions: ExecutionRegistry
    metrics: Metrics
    topology: dict
    trace_writer: AgentTestBenchTraceWriter
    _sandbox_scope_override: ResourceScope | None
    _completed_tool_event_ids: set[tuple[str | None, ...]]
    _model_event_ids: set[tuple[str | None, ...]]
    _tool_decisions_by_event_id: dict[
        tuple[str | None, ...],
        tuple[str, str, ToolDecision],
    ]
    _decision_tasks: dict[
        tuple[str | None, ...],
        asyncio.Task[ToolDecision],
    ]
    _runtime_activity: dict[tuple[str | None, str | None], int]
    _stage2_finalize_tasks: dict[str, asyncio.Task[None]]
    _recent_samples: list[dict[str, object]]  # recent tool runtime samples for /v1/tools/recent
    _sandbox_scopes_by_owner: dict[tuple[str | None, str], ResourceScope] = field(
        default_factory=dict
    )
    _max_recent_samples: int = 200  # max samples to keep in memory


def build_state(config: SchedulerConfig | None = None) -> AppState:
    cfg = config or SchedulerConfig.from_env()
    topology = read_topology(
        reserve_ratio=cfg.cpu_reserve_ratio,
        reserve_cores=cfg.cpu_reserve_cores,
        cpu_budget_cores=cfg.cpu_budget_cores,
    )
    detected_tool_budget = topology.get("tool_cpu_budget_cores")
    auto_active_tools = max(
        1,
        math.floor(
            float(detected_tool_budget)
            if isinstance(detected_tool_budget, (int, float))
            else 1.0
        ),
    )
    max_active_tools = cfg.max_global_concurrency or auto_active_tools
    topology["max_active_tools"] = max_active_tools
    topology["max_active_tools_source"] = (
        "configured" if cfg.max_global_concurrency else "effective_cpu_budget"
    )
    cpu_budget_mcpu = (
        max(1, round(float(detected_tool_budget) * 1_000))
        if topology.get("available") is True
        and isinstance(detected_tool_budget, (int, float))
        and float(detected_tool_budget) > 0.0
        else None
    )
    topology["tool_cpu_budget_mcpu"] = cpu_budget_mcpu
    leases = LeaseManager(
        max_active_tools,
        cfg.lease_ttl_ms,
        cpu_budget_mcpu=cpu_budget_mcpu,
    )
    predictor = ToolResourcePredictor.from_traces(
        openclaw_trace_paths=cfg.tool_resource_trace_paths,
        stage2_trace_paths=cfg.tool_resource_stage2_trace_paths,
        buckets=LatencyBuckets(cfg.tool_resource_latency_buckets_ms),
        repo=cfg.tool_resource_repo,
        artifact_dir=(
            cfg.tool_resource_artifact_dir
            if cfg.tool_resource_artifact_dir is not None
            else cfg.trace_dir / "tool-resource"
        ),
        container_executable=cfg.tool_resource_container_executable,
        ttl_by_bucket_s=cfg.tool_resource_ttl_by_bucket_s,
        miss_penalty_s=cfg.tool_resource_miss_penalty_s,
    )
    if predictor.artifact_dir is not None:
        predictor.artifact_dir.mkdir(parents=True, exist_ok=True)
    policy: SchedulingPolicy
    if cfg.policy == "concurrency":
        policy = ConcurrencyPolicy(leases, cfg.admission_wait_ms)
    else:
        policy = ObserveOnlyPolicy()
    tool_monitor = RealtimeToolMonitor(
        poll_interval_s=max(0.01, cfg.resource_poll_interval_ms / 1000),
        max_timeline_points=max(1, cfg.resource_timeline_max_points),
    )
    docker_exec_observer = (
        DockerExecObserver(
            enabled=cfg.docker_exec_observer_enabled,
            docker_socket=cfg.docker_socket,
            container_id=cfg.sandbox_container_id,
            container_prefix=cfg.docker_exec_container_prefix,
            cgroup_path=cfg.sandbox_cgroup_path,
            on_scope=tool_monitor.bind_scope,
        )
        if cfg.docker_exec_observer_enabled
        else None
    )
    return AppState(
        config=cfg,
        predictor=predictor,
        leases=leases,
        policy=policy,
        tool_monitor=tool_monitor,
        docker_exec_observer=docker_exec_observer,
        executions=ExecutionRegistry(),
        metrics=Metrics(),
        topology=topology,
        trace_writer=AgentTestBenchTraceWriter(
            cfg.trace_dir,
            max_messages_bytes=cfg.trace_max_messages_bytes,
            default_repo=cfg.tool_resource_repo,
        ),
        _sandbox_scope_override=None,
        _completed_tool_event_ids=set(),
        _model_event_ids=set(),
        _tool_decisions_by_event_id={},
        _decision_tasks={},
        _runtime_activity={},
        _stage2_finalize_tasks={},
        _recent_samples=[],
        _sandbox_scopes_by_owner={},
    )
