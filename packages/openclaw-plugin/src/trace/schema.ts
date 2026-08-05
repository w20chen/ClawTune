/**
 * Trace v6 schema types.
 *
 * Every line in a v6 trace JSONL file is a self-contained event with a
 * stable schema_version, record_type, span identity, and timestamps.
 */

export const TRACE_SCHEMA_VERSION = 6;

// ── Record Types ───────────────────────────────────────────────────────

export type TraceRecordType =
  | "trace_metadata"
  | "span_start"
  | "span_end";

// ── Span Kinds ─────────────────────────────────────────────────────────

export type SpanKind = "llm" | "tool";

// ── Status Codes ───────────────────────────────────────────────────────

export type StatusCode =
  | "ok"
  | "error"
  | "timeout"
  | "cancelled"
  | "interrupted"
  | "unknown";

// ── Execution Mode ─────────────────────────────────────────────────────

export type ExecutionMode =
  | "launcher"
  | "marker"
  | "in_process_or_runtime_managed";

// ── Attribution Status ─────────────────────────────────────────────────

export type AttributionStatus =
  | "attributed"
  | "partially_attributed"
  | "unattributed"
  | "failed"
  | "not_applicable"
  | "unknown";

// ── Monitor Quality ────────────────────────────────────────────────────

export type MonitorQuality =
  | "complete"
  | "partial"
  | "unknown";

// ── Coverage Reason ────────────────────────────────────────────────────

export type CoverageReason =
  | "full_window"
  | "pid_registered_late"
  | "monitor_stopped_early"
  | "pid_unavailable"
  | "internal_tool_no_process"
  | "shared_runtime_process"
  | "shared_sandbox_container"
  | "monitor_window_no_overlap"
  | "not_applicable"
  | "cgroup_unavailable"
  | "monitor_error"
  | "clock_data_missing";

// ── PID Role ───────────────────────────────────────────────────────────

export type PidRole = "payload_root" | "launcher" | "unknown";

// ── Correlation Status ─────────────────────────────────────────────────

export type CorrelationStatus = "resolved" | "unresolved";

// ── Shared Fields (present in both span_start and span_end) ────────────

export interface SpanIdentity {
  schema_version: typeof TRACE_SCHEMA_VERSION;
  record_type: TraceRecordType;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  session_id: string | null;
  run_id: string | null;
  agent_id: string | null;
  sequence_no: number;
  kind: SpanKind;
  name: string;
  wall_time_ns: string;   // bigint serialized as string for JSON safety
  monotonic_time_ns: string;
}

// ── Span Start ─────────────────────────────────────────────────────────

export interface SpanStartInput {
  /** The actual tool arguments as received by the before_tool_call hook. */
  requested_args: Record<string, unknown> | null;
  /** Snapshot of LLM input messages (for llm spans). */
  messages?: unknown[] | null;
}

export interface SpanStartExecution {
  mode: ExecutionMode | null;
  execution_id: string | null;
}

export interface SpanStartRecord extends SpanIdentity {
  record_type: "span_start";
  input: SpanStartInput;
  /** Optional scheduler-side prediction captured before tool execution. */
  prediction?: ToolPrediction | null;
  execution: SpanStartExecution;
  /** Optional: raw model-side tool call observation for debugging. */
  model_tool_call_observation?: ModelToolCallObservation | null;
  /** Optional: parent correlation diagnostics when unresolved. */
  correlation?: SpanCorrelation | null;
}

export interface ModelToolCallObservation {
  tool_call_id: string | null;
  raw_arguments: string | null;
  parse_status: "verified" | "damaged_or_unverified";
}

export interface ToolPrediction {
  duration_p50_ms: number | null;
  duration_p90_ms: number | null;
  resource_class: string;
  confidence: number | null;
  tool_resource?: unknown | null;
}

// ── Span End ───────────────────────────────────────────────────────────

export interface SpanEndStatus {
  code: StatusCode;
  message: string | null;
}

export interface SpanEndOutput {
  exit_code?: number | null;
  result?: unknown;
  /** For LLM spans: the model response content. */
  content?: unknown;
}

export interface SpanEndExecution {
  mode: ExecutionMode | null;
  execution_id: string | null;
  requested_command?: string | null;
  effective_command?: string | null;
  payload_command?: string | null;
  payload_pid?: number | null;
  payload_pid_start_time_ticks?: number | null;
  cgroup_path?: string | null;
  cgroup_id?: string | null;
  pid_role?: PidRole | null;
  tool_resource?: unknown | null;
}

export interface SpanEndResources {
  attribution_status: AttributionStatus;
  scope: "process_tree" | "cgroup" | "none";
  quality: MonitorQuality;

  monitor_start_wall_time_ns: string | null;
  monitor_end_wall_time_ns: string | null;
  monitor_start_monotonic_ns: string | null;
  monitor_end_monotonic_ns: string | null;

  coverage_duration_ns: string | null;
  action_duration_ns: string;
  coverage_ratio: number | null;

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
  /** Trace-relative path of the independent cgroup resource artifact. */
  cgroup_artifact_path?: string | null;

  coverage_reason: CoverageReason | string | null;

  cpu_time_s?: number | null;
  rss_peak_bytes?: number | null;
  memory_rss_bytes_before?: number | null;
  memory_rss_bytes_after?: number | null;
  disk_read_bytes_delta?: number | null;
  disk_write_bytes_delta?: number | null;
  net_rx_bytes_delta?: number | null;
  net_tx_bytes_delta?: number | null;
  ctx_switches_delta?: number | null;
  cpu_utilization_avg_cores?: number | null;
  cpu_utilization_avg_pct?: number | null;
  disk_read_bytes_per_s?: number | null;
  disk_write_bytes_per_s?: number | null;
  net_rx_bytes_per_s?: number | null;
  net_tx_bytes_per_s?: number | null;
  sampling_interval_ms?: number | null;
  sampling_point_count?: number | null;
  sampling_quality?: string | null;
  resource_timeline?: unknown[] | null;
  resource_timeline_truncated?: boolean | null;
  resource_class?: string | null;
  target_pid?: number | null;
  process_count_before?: number | null;
  process_count_after?: number | null;
  monitor_source?: string | null;
}

export interface SpanEndRecord extends SpanIdentity {
  record_type: "span_end";
  /** Duration computed from monotonic clock (hrtime). */
  duration_ns: string;
  /** Duration in seconds, derived from duration_ns / 1e9. */
  duration_sec: string;
  /** OpenClaw-reported duration in milliseconds, for cross-validation.
   *  Available when the hook provides duration_ms (model_call_ended,
   *  after_tool_call).  Null when unavailable. */
  observed_duration_ms?: number | null;
  status: SpanEndStatus;
  output: SpanEndOutput;
  execution: SpanEndExecution;
  resources: SpanEndResources;
  /** Optional: parent correlation diagnostics when unresolved. */
  correlation?: SpanCorrelation | null;
}

// ── Correlation Diagnostics ────────────────────────────────────────────

export interface SpanCorrelation {
  status: CorrelationStatus;
  reason?: string | null;
}

// ── Truncation Info ────────────────────────────────────────────────────

export interface TruncationInfo {
  [fieldPath: string]: {
    truncated: boolean;
    original_bytes: number;
    stored_bytes: number;
  };
}

// ── Trace Metadata ─────────────────────────────────────────────────────

export interface TraceMetadataRecord {
  schema_version: typeof TRACE_SCHEMA_VERSION;
  record_type: "trace_metadata";
  trace_format_version: typeof TRACE_SCHEMA_VERSION;
  scaffold: string;
  mode: "collect";
  created_at: string;
  /** Description of clock source used by the writer. */
  clock_source?: string;
  /** Precision of the clock (e.g. "microsecond" or "nanosecond"). */
  clock_precision?: string;
}

// ── Union ──────────────────────────────────────────────────────────────

export type TraceRecord =
  | TraceMetadataRecord
  | SpanStartRecord
  | SpanEndRecord;

// ── In-Memory Active Span ──────────────────────────────────────────────

export interface ActiveSpan {
  traceId: string;
  spanId: string;
  parentSpanId: string | null;
  sessionId: string | null;
  runId: string | null;
  agentId: string | null;
  sequenceNo: number;
  kind: SpanKind;
  name: string;
  startWallTimeNs: bigint;
  startMonotonicTimeNs: bigint;
  /** Optional: track whether we've written span_start to disk. */
  startWritten: boolean;
  /** Arbitrary metadata for cross-hook data passing. */
  metadata?: Record<string, unknown>;
}

// ── Trace Config ───────────────────────────────────────────────────────

export interface TraceConfig {
  schema_version: number;
  include_raw_events: boolean;
  include_llm_messages: boolean;
  include_tool_outputs: boolean;
  redact_sensitive_data: boolean;
  flush_span_start: boolean;
  max_string_bytes: number;
  max_messages_bytes: number;
  max_tool_output_bytes: number;
  /** Path to the trace JSONL output file. If empty/null, tracing is disabled. */
  trace_dir: string;
}

export const DEFAULT_TRACE_CONFIG: TraceConfig = {
  schema_version: 6,
  include_raw_events: false,
  include_llm_messages: true,
  include_tool_outputs: true,
  redact_sensitive_data: true,
  flush_span_start: true,
  max_string_bytes: 16384,
  max_messages_bytes: 131072,
  max_tool_output_bytes: 65536,
  trace_dir: "",  // disabled by default; scheduler is the primary writer
};
