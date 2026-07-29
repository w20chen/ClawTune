from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent_scheduler.admission.leases import LeaseManager
from agent_scheduler.config import SchedulerConfig
from agent_scheduler.contracts.models import ResourceScope
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
    _completed_tool_event_ids: set[str]  # dedup: track completed tool event_ids
    _stage2_finalize_tasks: dict[str, asyncio.Task[None]]
    _recent_samples: list[dict[str, object]]  # recent tool runtime samples for /v1/tools/recent
    _max_recent_samples: int = 200  # max samples to keep in memory


def build_state(config: SchedulerConfig | None = None) -> AppState:
    cfg = config or SchedulerConfig.from_env()
    leases = LeaseManager(cfg.max_global_concurrency, cfg.lease_ttl_ms)
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
        topology=read_topology(),
        trace_writer=AgentTestBenchTraceWriter(
            cfg.trace_dir,
            max_messages_bytes=cfg.trace_max_messages_bytes,
        ),
        _sandbox_scope_override=None,
        _completed_tool_event_ids=set(),
        _stage2_finalize_tasks={},
        _recent_samples=[],
    )
