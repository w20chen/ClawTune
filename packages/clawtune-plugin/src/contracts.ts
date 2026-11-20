/** Plugin configuration and API contract types. */

export type Mode = "observe" | "enforce";
export type ExecutionBackend = "hook-only" | "marker" | "managed-wrapper";
export type ProfilingMode = "off";

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
  logLevel: "error" | "warn" | "info" | "debug";
  /**
   * Console output mode for turn-by-turn logging.
   * - "verbose": Print LLM messages, tool calls, and tool results to stdout.
   * - "quiet": Suppress turn-by-turn console output (only internal logs).
   */
  consoleMode: "verbose" | "quiet";
  executionBackend: ExecutionBackend;
  /**
   * When true and executionBackend is "hook-only", the plugin still mints a
   * per-call execution_id and wraps the exec command in a ClawBox SSH bridge
   * envelope (`__CBX_EXEC_1__` header line).  The tool bridge adopts that
   * execution_id so the ClawTune span and the bridge execution record share an
   * exact join key (no time-window heuristic).  Default: false.
   */
  sandboxExecEnvelope: boolean;
  sandboxExecPredictionModel: "tool" | "lattice";
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
   * When true, the plugin will automatically start the ClawTune sidecar
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
   * Shell command to start the ClawTune sidecar.  Used only when
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
   * (git remote "origin", then directory basename).  `CLAWTUNE_REPO_KEY` env
   * still takes precedence over this value (swe-rebench sets it per task).
   */
  repo?: string | null;
  trace: TracePluginConfig;
};

export type CommonEvent = {
  schema_version: "clawtune.v1";
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
  command_context?: Record<string, string | null> | null;
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
  prediction_signal_id?: string | null;
  decision_id: string;
  action: "allow" | "block";
  reason_code: string;
  reason: string;
  policy_name: string;
  policy_version: string;
  lease_id: string | null;
  prediction: {
    tool: {load: CallLoadPrediction; pmu: PmuPrediction};
    trie: {load: CallLoadPrediction};
    lattice: {load: CallLoadPrediction};
  };
  placement?: unknown | null;
  profiling?: unknown | null;
};

// Mirrors contracts/tool-prediction.schema.json and call-load.schema.json.
export type LoadBackend = "runtime" | "trie" | "lattice";
export type LoadTarget = "duration_ms" | "cpu_time_seconds" | "cpu_avg_cores" | "cpu_peak_cores" | "sampled_peak_rss_bytes" | "memory_total_peak_bytes" | "memory_extra_peak_bytes";
export type LoadEstimate = {
  status: "available" | "unavailable";
  unit: "ms" | "core_seconds" | "cores" | "bytes";
  metric_definition: string;
  avg: number | null;
  p50: number | null;
  p90: number | null;
  buckets: { edges: number[]; interval: "left_closed_right_open"; probabilities: number[] | null };
  backend: LoadBackend;
  method: "direct" | "composed" | "unavailable";
  evidence_counts: number[];
  sample_count: number;
  context: string[];
  assumptions: string[];
  calibration: "unvalidated";
  unavailable_reason: string | null;
};
export type CallLoadPrediction = {
  schema_version: "call_load.v2";
  memory_measurement: "cgroup_v2_memory_current" | "cgroup_v2_environment_union_v1" | "guest_memtotal_minus_memavailable";
  scope: "tool_call";
  lifecycle: "tool_hook_interval";
  cpu_peak_window_ms: 500;
  quantile_method: "median_p50_nearest_rank_p90";
  targets: Record<LoadTarget, LoadEstimate>;
  clause_predictions?: Array<{
    quantile_method?: "median_p50_nearest_rank_p90";
    clause_index: number; argv: string[]; cwd: string | null; env_names: string[];
    memory_measurement: "cgroup_v2_memory_current" | "cgroup_v2_environment_union_v1" | "guest_memtotal_minus_memavailable";
    scope: "clause"; targets: Record<LoadTarget, LoadEstimate>;
  }>;
};
export type PmuTarget = "cycles" | "instructions" | "llc_read_accesses" | "llc_read_misses" | "llc_read_accesses_per_cpu_second" | "llc_read_misses_per_cpu_second" | "ipc" | "llc_mpki" | "llc_miss_rate";
export type PmuEstimate = {
  status: "available" | "unavailable";
  unit: "instructions_per_cycle" | "misses_per_kilo_instructions" | "ratio" | "count" | "accesses_per_cpu_second" | "misses_per_cpu_second";
  metric_definition: string;
  avg: number | null;
  p50: number | null;
  p90: number | null;
  backend: "runtime";
  method: "direct" | "unavailable";
  evidence_count: number;
  context: string[];
  calibration: "unvalidated";
  unavailable_reason: string | null;
};
export type PmuPrediction = {
  schema_version: "pmu_prediction.v2";
  scope: "tool_call";
  lifecycle: "completed_execution_profile";
  quantile_method: "median_p50_nearest_rank_p90";
  targets: Record<PmuTarget, PmuEstimate>;
};

export type ToolCompletedEvent = CommonEvent & {
  tool_call_id: string | null;
  decision_id: string | null;
  lease_id: string | null;
  execution_id: string | null;
  tool_name: string;
  duration_ms: number;
  confirmed_timeout?: boolean;
  timeout_duration_lower_bound_ms?: number | null;
  /** Plugin-observed full before->after tool hook window (ns). */
  plugin_window_ns?: string | null;
  /** OpenClaw-reported tool action duration (ns). */
  tool_body_ns?: string | null;
  /** Producer action boundaries in the plugin process monotonic clock. */
  action_start_monotonic_ns?: string | null;
  action_end_monotonic_ns?: string | null;
  monotonic_clock_domain?: "linux_monotonic" | "openclaw_plugin_process_monotonic" | null;
  /** Plugin-measured before-hook sidecar round-trip overhead (ns). */
  decision_duration_ns?: string | null;
  /** Plugin-measured after-hook sidecar round-trip overhead (ns). */
  completion_duration_ns?: string | null;
  /** Plugin-measured sidecar/plugin round-trip overhead (ns), outside the action. */
  sidecar_overhead_ns?: string | null;
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
  prediction_id?: string | null;
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

/** Health-check payload returned by GET /health/live and /health/ready. */
export type SidecarHealth = {
  kb_owner?: string | null;
  schema_version: string;
  service: string;
  sidecar_version?: string;
  protocol_versions?: string[];
  live?: boolean;
  ready?: boolean;
};

/** Minimum compatible sidecar version. The plugin refuses to connect to older sidecars. */
export const MIN_COMPATIBLE_SIDECAR_VERSION = "0.1.0";

/** Protocol versions the plugin expects from the sidecar. */
export const REQUIRED_PROTOCOL_VERSIONS = ["clawtune.api.v1", "execution.v1", "clawtune.signal.v1", "clawtune.prediction.v1"];

/** Clause-lineage sampled RSS and CPU; never whole-call/cgroup estimates. */
