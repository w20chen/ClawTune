from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "scheduler.v1"


class CommonEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["scheduler.v1"]
    event_id: str
    occurred_at: str
    plugin_version: str
    run_id: str | None
    session_id: str | None
    session_key: str | None
    agent_id: str | None


class ParamFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serialized_size_bytes: int = Field(ge=0)
    string_length: int = Field(ge=0)
    list_item_count: int = Field(ge=0)
    path_count: int = Field(ge=0)
    has_command_like_field: bool


class ResourceScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["pid", "cgroup-v2"] = "pid"
    execution_id: str | None = None
    pid: int | None = Field(default=None, ge=0)
    root_pid: int | None = Field(default=None, ge=0)
    process_start_time: float | None = Field(default=None, ge=0)
    root_starttime_ticks: float | None = Field(default=None, ge=0)
    cgroup_path: str | None = None
    pid_namespace_inode: int | None = Field(default=None, ge=0)
    container_id: str | None = None
    include_children: bool = True
    source: str | None = None
    attribution_source: str | None = None


class ToolBeforeRequest(CommonEvent):
    tool_call_id: str | None
    tool_name: str
    tool_kind: str | None
    tool_input_kind: str | None
    operation_hint: str | None = None
    derived_paths: list[str]
    params_digest: str
    param_features: ParamFeatures
    raw_params: Any | None = None
    raw_event: Any | None = None
    resource_scope: ResourceScope | None = None


class ToolPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_p50_ms: int | None = Field(default=None, ge=0)
    duration_p90_ms: int | None = Field(default=None, ge=0)
    resource_class: str = "unknown"
    confidence: float | None = Field(default=None, ge=0, le=1)
    tool_resource: Any | None = None


class PlacementAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu_set: str | None = None
    numa_node: int | None = Field(default=None, ge=0)
    llc_cluster: str | None = None
    advisory: Literal[True] = True


class ToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    action: Literal["allow", "block"]
    reason_code: str
    reason: str
    policy_name: str
    policy_version: str
    lease_id: str | None
    prediction: ToolPrediction
    placement_advice: PlacementAdvice
    placement: Any | None = None
    profiling: Any | None = None


class ToolCompletedEvent(CommonEvent):
    tool_call_id: str | None
    decision_id: str | None
    lease_id: str | None
    execution_id: str | None = None
    tool_name: str
    duration_ms: int = Field(ge=0)
    succeeded: bool
    error_type: str | None
    error_digest: str | None
    result_size_bytes: int | None = Field(default=None, ge=0)
    raw_result: Any | None = None
    raw_event: Any | None = None
    resource_scope: ResourceScope | None = None


class ModelEvent(CommonEvent):
    event_type: Literal["model_call_started", "model_call_ended"]
    call_id: str | None
    provider: str | None
    model: str | None
    duration_ms: int | None = Field(default=None, ge=0)
    outcome: str | None
    context_token_budget: int | None = Field(default=None, ge=0)
    raw_input: Any | None = None
    raw_output: Any | None = None
    raw_event: Any | None = None


class StatusResponse(BaseModel):
    ready: bool
    policy: str
    topology: dict[str, Any]


class ExecutionRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    tool_call_id: str | None
    run_id: str | None
    session_key_hash: str | None
    command_digest: str
    command: str
    workdir: str | None = None
    host: str = "gateway"
    placement: Any | None = None
    profiling: Any | None = None
    backend: Literal["marker", "managed-wrapper"]


class ExecutionRegistrationResponse(BaseModel):
    execution_id: str
    one_time_token: str
    expires_at: str


class ExecutionScopeResponse(BaseModel):
    execution_scope: ResourceScope | None


class ExecutionClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    token: str
    launcher_pid: int | None = Field(default=None, ge=0)


class ExecutionClaimResponse(BaseModel):
    execution_id: str
    update_token: str
    command: str
    command_digest: str
    workdir: str | None
    host: str
    placement: Any | None
    profiling: Any | None


class ExecutionStartedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    update_token: str
    launcher_pid: int = Field(ge=0)
    child_pid: int = Field(ge=0)
    process_starttime_ticks: int | None = Field(default=None, ge=0)
    cgroup_path: str | None = None
    pid_namespace_inode: int | None = Field(default=None, ge=0)
    container_id: str | None = None


class ExecutionExitedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    update_token: str
    exit_code: int | None = None
    signal: int | None = Field(default=None, ge=0)


class ExecutionUpdateResponse(BaseModel):
    stored: bool
