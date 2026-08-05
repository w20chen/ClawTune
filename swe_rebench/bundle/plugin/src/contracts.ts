/** Plugin configuration and API contract types. */

export type Mode = "observe" | "enforce";
export type ExecutionBackend = "hook-only" | "marker" | "managed-wrapper";
export type ProfilingMode = "off" | "proc" | "perf" | "ksys" | "vtune";

export type TracePluginConfig = {
  schema_version: number;
  include_raw_events: boolean;
  include_llm_messages: boolean;
  include_tool_outputs: boolean;
  redact_sensitive_data: boolean;
  flush_span_start: boolean;
  max_string_bytes: number;
  max_messages_bytes: number;
  max_tool_output_bytes: number;
  trace_dir: string;
};

export type PluginConfig = {
  endpoint: string;
  mode: Mode;
  decisionTimeoutMs: number;
  reportTimeoutMs: number;
  failOpen: boolean;
  sendRawParams: boolean;
  recordRawTrace: boolean;
  logLevel: "error" | "warn" | "info" | "debug";
  /**
   * Console output mode for turn-by-turn logging.
   * - "verbose": Print LLM messages, tool calls, and tool results to stdout.
   * - "quiet": Suppress turn-by-turn console output (only internal logs).
   */
  consoleMode: "verbose" | "quiet";
  executionBackend: ExecutionBackend;
  launcherPath: string;
  launcherInterpreter: string | null;
  collectorSocket: string;
  instrumentHosts: string[];
  instrumentTools: string[];
  enableCgroup: boolean;
  enableAffinity: boolean;
  enableNuma: boolean;
  profilingMode: ProfilingMode;
  securityBoundaryAccepted: boolean;
  /**
   * When true, the plugin will automatically start the scheduler sidecar
   * if it is not already running on the configured endpoint.  Default: false.
   * ClawTune setup enables this after its privileged eBPF preflight passes.
   */
  autoStartSidecar: boolean;
  /**
   * Maximum time allowed for sudo, Python/BCC initialization, and sidecar
   * readiness. Kunpeng cold starts can take several seconds. Default: 60000.
   */
  sidecarStartupTimeoutMs: number;
  /**
   * Shell command to start the scheduler sidecar.  Used only when
   * autoStartSidecar is true.  The command is executed via the system
   * shell and should start the sidecar on the configured endpoint.
   *
   * Default: auto-detected from the project layout. On Linux checkouts with a
   * validated .venv this builds a direct sudo argv at runtime; no checkout
   * path is persisted in OpenClaw config.
   */
  sidecarCommand: string;
  /**
   * Optional explicit KB repo namespace for every event this runtime emits.
   * When unset the plugin derives one from the process working directory
   * (git remote "origin", then directory basename).  `CLAW_REPO_KEY` env
   * still takes precedence over this value (swe-rebench sets it per task).
   */
  repo?: string | null;
  trace: TracePluginConfig;
};

export type CommonEvent = {
  schema_version: "scheduler.v1";
  event_id: string;
  occurred_at: string;
  plugin_version: string;
  run_id: string | null;
  session_id: string | null;
  session_key: string | null;
  agent_id: string | null;
  gateway_id?: string | null;
  runtime_id?: string | null;
  repo?: string | null;
};

export type ResourceScope = {
  pid: number | null;
  process_start_time: number | null;
  container_id: string | null;
  include_children: boolean;
  source: string | null;
  kind?: "pid" | "cgroup-v2";
  execution_id?: string | null;
  root_pid?: number | null;
  root_starttime_ticks?: number | null;
  cgroup_path?: string | null;
  pid_namespace_inode?: number | null;
  attribution_source?: string | null;
};

export type ToolBeforeRequest = CommonEvent & {
  tool_call_id: string | null;
  tool_name: string;
  tool_kind: string | null;
  tool_input_kind: string | null;
  operation_hint: string | null;
  derived_paths: string[];
  params_digest: string;
  param_features: {
    serialized_size_bytes: number;
    string_length: number;
    list_item_count: number;
    path_count: number;
    has_command_like_field: boolean;
  };
  raw_params: unknown | null;
  raw_event: unknown | null;
  resource_scope: ResourceScope | null;
};

export type ToolDecision = {
  decision_id: string;
  action: "allow" | "block";
  reason_code: string;
  reason: string;
  policy_name: string;
  policy_version: string;
  lease_id: string | null;
  prediction: {
    duration_p50_ms: number | null;
    duration_p90_ms: number | null;
    resource_class: string;
    confidence: number | null;
    tool_resource?: ToolResourceCommandPrediction | null;
  };
  placement_advice: {
    cpu_set: string | null;
    numa_node: number | null;
    llc_cluster: string | null;
    advisory: true;
  };
  placement?: unknown | null;
  profiling?: unknown | null;
};

export type ToolResourceCommandPrediction = {
  repo: string;
  command: string;
  parse_failed: boolean;
  clause_bins: string[];
  clause_predictions: ToolResourceClausePredictionOutcome[];
  prediction: ToolResourceClausePrediction | null;
  unavailable_reason: string | null;
  continuous_predictions?: Record<string, ToolResourceContinuousPrediction> | null;
  lattice_time_predictions?: ToolResourceClauseLatticeTimePredictions[];
  prediction_algorithms?: ToolResourcePredictionAlgorithms | null;
};

export type ToolResourceClausePredictionOutcome = {
  clause_index: number;
  bin: string;
  argv: string[];
} & (
  | {
      prediction: ToolResourceClausePrediction;
      unavailable_reason: null;
    }
  | {
      prediction: null;
      unavailable_reason: string;
    }
);

export type ToolResourceClausePrediction = {
  bucket_id: number;
  probability_by_bucket: number[];
  scope: string;
  key_kind: string;
  evidence_count: number;
  fallback_path: string[];
};

export type ToolResourceContinuousPrediction = {
  target: string;
  conditional_p90: number | null;
  scope: string | null;
  key_kind: string | null;
  evidence_count: number;
  fallback_path: string[];
  note: string | null;
};

export type ToolResourceClauseLatticeTimePredictions = {
  clause_index: number;
  bin: string;
  argv: string[];
  predictions: ToolResourceLatticeTimePrediction[];
};

export type ToolResourceLatticeTimePrediction = {
  algorithm: "shrinkage" | "loso" | "max_cardinality";
  selected_features: string[];
  evidence_count: number;
  selected_risk: number | null;
  fallback: string | null;
} & (
  | {
      prediction_ms: number;
      exact_match: boolean;
      unavailable_reason: null;
    }
  | {
      prediction_ms: null;
      exact_match: null;
      unavailable_reason: string;
    }
);

export type ToolResourcePredictionAlgorithms = {
  enabled?: Array<{
    name?: string;
    family?: string;
    source?: string;
    targets?: string[];
    outputs?: string[];
  }>;
  excluded?: Array<{
    name?: string;
    source?: string;
    reason?: string;
  }>;
};

export type ToolCompletedEvent = CommonEvent & {
  tool_call_id: string | null;
  decision_id: string | null;
  lease_id: string | null;
  execution_id: string | null;
  tool_name: string;
  duration_ms: number;
  /** Plugin-observed full before->after tool hook window (ns). */
  plugin_window_ns?: string | null;
  /** OpenClaw-reported tool action duration (ns). */
  tool_body_ns?: string | null;
  /** Plugin-measured before-hook sidecar round-trip overhead (ns). */
  decision_duration_ns?: string | null;
  /** Plugin-measured after-hook sidecar round-trip overhead (ns). */
  completion_duration_ns?: string | null;
  /** Plugin-measured scheduler/plugin round-trip overhead (ns), outside the action. */
  scheduler_overhead_ns?: string | null;
  succeeded: boolean;
  error_type: string | null;
  error_digest: string | null;
  result_size_bytes: number | null;
  raw_result: unknown | null;
  raw_event: unknown | null;
  resource_scope: ResourceScope | null;
};

export type ExecutionTelemetryResponse = {
  tool_resource: unknown | null;
};

export type ModelEvent = CommonEvent & {
  event_type: "model_call_started" | "model_call_ended";
  call_id: string | null;
  provider: string | null;
  model: string | null;
  duration_ms: number | null;
  outcome: string | null;
  context_token_budget: number | null;
  raw_input: unknown | null;
  raw_output: unknown | null;
  raw_event: unknown | null;
};

export type ExecutionRegistrationRequest = {
  execution_id: string;
  gateway_id?: string | null;
  runtime_id?: string | null;
  repo?: string | null;
  agent_id?: string | null;
  session_id?: string | null;
  tool_call_id: string | null;
  lease_id?: string | null;
  run_id: string | null;
  session_key_hash: string | null;
  command_digest: string;
  command: string;
  workdir: string | null;
  host: string;
  placement: unknown | null;
  profiling: unknown | null;
  backend: "marker" | "managed-wrapper";
};

export type ExecutionRegistrationResponse = {
  execution_id: string;
  one_time_token: string;
  expires_at: string;
};
