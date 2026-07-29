import {definePluginEntry, type HookApi} from "openclaw/plugin-sdk/plugin-entry";
import {randomUUID} from "node:crypto";
import {readFileSync} from "node:fs";
import {SidecarClient} from "./client.js";
import {loadConfig, isRecord} from "./config.js";
import {CorrelationMap} from "./correlation.js";
import type {CommonEvent, ModelEvent, PluginConfig, ToolBeforeRequest, ToolCompletedEvent} from "./contracts.js";
import type {ResourceScope} from "./contracts.js";
import {buildTrustedResourceScope, instrumentExecParams} from "./exec-instrumentation.js";
import type {InstrumentResult} from "./exec-instrumentation.js";
import {consoleLogger} from "./logging.js";
import {jsonSafe, paramFeatures, redact, stableDigest} from "./redaction.js";
import {normalizeSandboxToolParams} from "./sandbox-paths.js";
import {
  SpanRegistry,
} from "./trace/registry.js";
import {
  TraceWriter,
} from "./trace/writer.js";
import {
  monotonicNowNs,
  wallClockNowNs,
  durationNs,
  CLOCK_SOURCE_DESCRIPTION,
  CLOCK_PRECISION,
} from "./trace/clock.js";
import {
  sanitizeTraceData,
} from "./trace/sanitizer.js";
import {extractToolExitCode, traceExitCodeForTool} from "./tool-result.js";
import type {
  SpanStartRecord,
  SpanEndRecord,
  TraceMetadataRecord,
  SpanKind,
  StatusCode,
  ExecutionMode,
  AttributionStatus,
  MonitorQuality,
  CoverageReason,
  SpanEndExecution,
  SpanEndResources,
} from "./trace/schema.js";
import { TRACE_SCHEMA_VERSION } from "./trace/schema.js";

const pluginVersion = "0.1.1";

// ── Plugin-wide state ──────────────────────────────────────────────────
let registry: SpanRegistry | null = null;

/** Unique instance ID generated once per plugin load (�?per CLI launch). */
const instanceId = randomUUID();

/** Per-run trace writers, keyed by normalized run identity. */
const runWriters = new Map<string, TraceWriter>();

/** Pending writer creation promises to prevent concurrent creation races. */
const pendingWriters = new Map<string, Promise<TraceWriter | null>>();

function safeFilename(segment: string | null): string {
  if (!segment) return "unknown";
  return segment.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 64);
}

/**
 * Get or create a trace writer for a run.
 *
 * Keys writers by runId (primary) or sessionId (fallback).
 * NEVER uses plugin instanceId as a key to prevent cross-run
 * accumulation in the same file.
 *
 * Returns null when neither runId nor sessionId is available
 * (trace data cannot be attributed to a specific run/session).
 */
async function getRunWriter(
  traceDir: string,
  runId: string | null,
  sessionId: string | null,
  agentId: string | null,
  logger: { warn(message: string, data?: unknown): void },
  flushSpanStart: boolean,
): Promise<TraceWriter | null> {
  const key = runId ?? sessionId;
  if (!key) {
    logger.warn("trace: skipping write, no run_id or session_id available", {
      runId,
      sessionId,
      agentId,
      instanceId,
    });
    return null;
  }

  const existing = runWriters.get(key);
  if (existing) return existing;

  // Prevent concurrent creation: if another caller is already creating
  // a writer for this key, wait for it and return the same writer.
  const pending = pendingWriters.get(key);
  if (pending) return pending;

  const promise = (async (): Promise<TraceWriter | null> => {
    // Double-check after acquiring the creation slot
    const recheck = runWriters.get(key);
    if (recheck) return recheck;

    const session = safeFilename(sessionId);
    const run = safeFilename(runId);
    // Note: agent_id is included per-record in the JSONL content.
    // It is omitted from the filename because model hooks (model_call_started,
    // model_call_ended) do not expose agent_id �?an OpenClaw limitation.
    const filename = `${session}_${run}.jsonl`;
    const { join } = await import("node:path");
    const filePath = join(traceDir, filename);

    const traceLogger = { warn: (msg: string, d?: unknown) => logger.warn(msg, d), info: (_msg: string, _d?: unknown) => {}, error: (_msg: string, _d?: unknown) => {} };
    const w = new TraceWriter(filePath, flushSpanStart, traceLogger);
    await w.open();

    const metadata: TraceMetadataRecord = {
      schema_version: TRACE_SCHEMA_VERSION,
      record_type: "trace_metadata",
      trace_format_version: TRACE_SCHEMA_VERSION,
      scaffold: "openclaw",
      mode: "collect",
      created_at: new Date().toISOString().replace("+00:00", "Z"),
      clock_source: CLOCK_SOURCE_DESCRIPTION,
      clock_precision: CLOCK_PRECISION,
    };
    w.writeRecord(metadata);

    runWriters.set(key, w);
    return w;
  })();

  pendingWriters.set(key, promise);
  try {
    return await promise;
  } finally {
    pendingWriters.delete(key);
  }
}

export default definePluginEntry({
  id: "agent-scheduler",
  name: "Agent Scheduler",
  description: "Agent scheduling and tracing bridge for OpenClaw.",
  configSchema: {
    type: "object",
    additionalProperties: false,
    properties: {
      endpoint: {type: "string", default: "http://localhost:8765"},
      mode: {enum: ["observe", "enforce"], default: "observe"},
      decisionTimeoutMs: {type: "integer", default: 800, minimum: 1},
      reportTimeoutMs: {type: "integer", default: 800, minimum: 1},
      failOpen: {type: "boolean", default: true},
      sendRawParams: {type: "boolean", default: false},
      recordRawTrace: {type: "boolean", default: false},
      logLevel: {enum: ["error", "warn", "info", "debug"], default: "info"},
      consoleMode: {enum: ["verbose", "quiet"], default: "verbose"},
      executionBackend: {enum: ["hook-only", "marker", "managed-wrapper"], default: "managed-wrapper"},
      launcherPath: {type: "string", default: "/opt/claw/bin/claw-launch"},
      launcherInterpreter: {type: ["string", "null"], default: null},
      collectorSocket: {type: "string", default: "/run/claw/collector.sock"},
      instrumentHosts: {type: "array", items: {type: "string"}, default: ["gateway"]},
      instrumentTools: {type: "array", items: {type: "string"}, default: ["exec"]},
      enableCgroup: {type: "boolean", default: true},
      enableAffinity: {type: "boolean", default: true},
      enableNuma: {type: "boolean", default: true},
      profilingMode: {enum: ["off", "proc", "perf", "ksys", "vtune"], default: "off"},
      securityBoundaryAccepted: {type: "boolean", default: true},
      trace: {
        type: "object",
        additionalProperties: false,
        properties: {
          schema_version: {type: "integer", default: 6},
          include_raw_events: {type: "boolean", default: false},
          include_llm_messages: {type: "boolean", default: true},
          include_tool_outputs: {type: "boolean", default: true},
          redact_sensitive_data: {type: "boolean", default: true},
          flush_span_start: {type: "boolean", default: true},
          max_string_bytes: {type: "integer", default: 16384},
          max_messages_bytes: {type: "integer", default: 131072},
          max_tool_output_bytes: {type: "integer", default: 65536},
          // Default disabled: the Python scheduler is the primary trace
          // writer. Set to a path (e.g. "traces/") to enable plugin-side
          // trace writing as a fallback.  Uses append mode so it won't
          // clobber scheduler-written data.
          trace_dir: {type: "string", default: ""},
        },
      },
    }
  },
  register(api: HookApi): void {
  const config = loadConfig(api.pluginConfig ?? {});
  const logger = api.logger ?? consoleLogger;
  const client = new SidecarClient(config);
  const correlation = new CorrelationMap(300_000, 10_000);

  // Initialize trace v6 if trace_dir is configured
  registry = new SpanRegistry();
  const traceCfg = config.trace;

  // ── Console turn-by-turn logging (verbose mode) ──────────────────
  const CONSOLE_PREFIX = "[openclaw]";
  let turnCounter = 0;
  const pendingToolNames = new Map<string, string>(); // toolCallId -> toolName

  function consoleVerbose(msg: string): void {
    if (config.consoleMode !== "verbose") return;
    console.log(`${CONSOLE_PREFIX} ${msg}`);
  }

  function truncateStr(s: string, maxLen: number): string {
    if (s.length <= maxLen) return s;
    return s.slice(0, maxLen) + `...<truncated ${s.length - maxLen} chars>`;
  }

  function summarizeMessages(messages: unknown): string {
    if (!Array.isArray(messages)) return "? messages";
    const roles = new Map<string, number>();
    for (const m of messages) {
      if (!isRecord(m)) continue;
      const role = String((m as Record<string, unknown>).role ?? "unknown");
      roles.set(role, (roles.get(role) ?? 0) + 1);
    }
    const parts = Array.from(roles.entries()).map(([r, c]) => `${r}×${c}`);
    return parts.length > 0 ? parts.join(", ") : `${messages.length} messages`;
  }

  function extractTextContent(output: unknown): string | null {
    if (!isRecord(output)) return null;
    const o = output as Record<string, unknown>;
    // OpenAI style: choices[0].message.content
    const choices = o.choices;
    if (Array.isArray(choices) && choices.length > 0 && isRecord(choices[0])) {
      const msg = (choices[0] as Record<string, unknown>).message ?? (choices[0] as Record<string, unknown>).delta;
      if (isRecord(msg)) {
        const content = (msg as Record<string, unknown>).content;
        if (typeof content === "string" && content.length > 0) return content;
      }
    }
    // Direct content field
    const content = o.content;
    if (typeof content === "string" && content.length > 0) return content;
    // text field
    const text = o.text;
    if (typeof text === "string" && text.length > 0) return text;
    return null;
  }

  function extractToolCallsForDisplay(output: unknown): Array<{name: string; id: string}> {
    const result: Array<{name: string; id: string}> = [];
    if (!isRecord(output)) return result;
    const o = output as Record<string, unknown>;

    // OpenAI style: choices[0].message.tool_calls
    const choices = o.choices;
    if (Array.isArray(choices) && choices.length > 0 && isRecord(choices[0])) {
      const msg = (choices[0] as Record<string, unknown>).message ?? (choices[0] as Record<string, unknown>).delta;
      if (isRecord(msg)) {
        const toolCalls = (msg as Record<string, unknown>).tool_calls;
        if (Array.isArray(toolCalls)) {
          for (const tc of toolCalls) {
            if (!isRecord(tc)) continue;
            const tcRec = tc as Record<string, unknown>;
            const fn = tcRec.function;
            const name = isRecord(fn) ? String((fn as Record<string, unknown>).name ?? "?") : "?";
            const id = String(tcRec.id ?? "");
            result.push({name, id});
          }
        }
        return result;
      }
    }
    // Direct tool_calls
    const directCalls = o.tool_calls;
    if (Array.isArray(directCalls)) {
      for (const tc of directCalls) {
        if (!isRecord(tc)) continue;
        const tcRec = tc as Record<string, unknown>;
        const fn = tcRec.function;
        const name = isRecord(fn) ? String((fn as Record<string, unknown>).name ?? "?") : "?";
        const id = String(tcRec.id ?? "");
        result.push({name, id});
      }
    }
    return result;
  }

  function summarizeToolParams(event: unknown): string {
    if (!isRecord(event)) return "";
    const params = (event as Record<string, unknown>).params ?? (event as Record<string, unknown>).arguments ?? (event as Record<string, unknown>).input;
    if (params === null || params === undefined) return "";
    if (typeof params === "string") return truncateStr(params, 200);
    if (isRecord(params)) {
      const keys = Object.keys(params as Record<string, unknown>);
      if (keys.length === 0) return "{}";
      // For known tools, print key fields
      const p = params as Record<string, unknown>;
      const cmd = p.command ?? p.cmd;
      if (typeof cmd === "string") return `command="${truncateStr(cmd, 150)}"`;
      const filePath = p.file_path ?? p.path ?? p.filePath ?? p.file;
      if (typeof filePath === "string") {
        const content = p.content ?? p.text;
        const contentLen = typeof content === "string" ? ` (${content.length} chars)` : "";
        return `path="${filePath}"${contentLen}`;
      }
      // Generic: list key=value pairs
      const entries = keys.slice(0, 3).map(k => {
        const v = p[k];
        const vs = typeof v === "string" ? truncateStr(v, 60) : (typeof v === "object" ? "{...}" : String(v));
        return `${k}=${vs}`;
      });
      return entries.join(", ") + (keys.length > 3 ? ` +${keys.length - 3} more` : "");
    }
    return truncateStr(String(params), 200);
  }

  function summarizeToolResult(event: unknown): string {
    if (!isRecord(event)) return "";
    const result = (event as Record<string, unknown>).result ?? (event as Record<string, unknown>).output ?? (event as Record<string, unknown>).response;
    if (result === null || result === undefined) return "(no output)";
    if (typeof result === "string") return truncateStr(result, 300);
    if (isRecord(result)) {
      const r = result as Record<string, unknown>;
      const text = r.text ?? r.content ?? r.message ?? r.stdout;
      if (typeof text === "string") return truncateStr(text, 300);
      return truncateStr(JSON.stringify(result), 300);
    }
    return truncateStr(String(result), 300);
  }

  // ── Debug: dump OpenClaw hook payload keys (once per hook type) ──
  const debugDumped = new Set<string>();
  function dumpHookShape(event: unknown, context: unknown, hookName: string): void {
    if (config.logLevel !== "debug") return;
    if (debugDumped.has(hookName)) return;
    debugDumped.add(hookName);
    const evtKeys = isRecord(event) ? Object.keys(event as Record<string, unknown>) : [];
    const ctxKeys = isRecord(context) ? Object.keys(context as Record<string, unknown>) : [];
    const runId = extractString(event, ["run_id", "runId"]) ?? extractString(context, ["runId", "run_id"]);
    const sessionId = extractString(event, ["session_id", "sessionId"]) ?? extractString(context, ["sessionId", "session_id"]);
    const agentId = extractString(event, ["agent_id", "agentId"]) ?? extractString(context, ["agentId", "agent_id"]);
    // Inline key fields in the message because OpenClaw's logger
    // only renders the message string, not the structured data.
    logger.warn(
      `[trace debug] ${hookName} | ` +
      `run_id=${runId ?? "-"} session_id=${sessionId ?? "-"} agent_id=${agentId ?? "-"} | ` +
      `event_keys=[${evtKeys.join(",")}] context_keys=[${ctxKeys.join(",")}] | ` +
      `fallback_instanceId=${instanceId}`
    );
  }

  // ── before_tool_call ──────────────────────────────────────────────

  api.on("before_tool_call", async (event: unknown, context: unknown) => {
    dumpHookShape(event, context, "before_tool_call");
    const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
    const toolCallId = extractString(event, ["tool_call_id", "toolCallId", "id"]);

    // ── verbose console: tool call ──
    if (toolCallId) pendingToolNames.set(toolCallId, toolName);
    const paramStr = summarizeToolParams(event);
    consoleVerbose(`■ ${toolName}${paramStr ? ` ${paramStr}` : ""}`);

    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const runId = extractString(event, ["run_id", "runId"]) ?? extractString(context, ["runId", "run_id"]);
    const sessionId = extractString(event, ["session_id", "sessionId"]) ?? extractString(context, ["sessionId", "session_id"]);
    const agentId = extractString(event, ["agent_id", "agentId"]) ?? extractString(context, ["agentId", "agent_id"]);
    const traceId = runId ?? sessionId ?? "unknown-run";

    // Resolve parent span
    let parentSpanId: string | null = null;
    let correlationStatus: "resolved" | "unresolved" = "unresolved";
    let correlationReason: string | null = null;
    if (toolCallId && registry) {
      parentSpanId = registry.getToolCallParent(toolCallId);
      if (parentSpanId) {
        correlationStatus = "resolved";
      } else {
        correlationReason = "tool_call_id_not_found";
      }
    } else {
      correlationReason = "no_tool_call_id";
    }

    // Generate span ID
    const spanId = toolCallId ?? `${traceId}:tool:${registry ? String(registry.listActiveSpans().length) : "0"}`;

    // Build span_start
    const startWall = wallClockNowNs();
    const startMono = monotonicNowNs();

    if (registry) {
      registry.beginSpan({
        traceId,
        spanId,
        parentSpanId,
        sessionId,
        runId,
        agentId,
        kind: "tool",
        name: toolName,
        startWallTimeNs: startWall,
        startMonotonicTimeNs: startMono,
      });
    }

    // Build input args from before hook (the true source of truth)
    const hookParams = isRecord(event) ? (event as Record<string, unknown>).params ?? (event as Record<string, unknown>).arguments ?? (event as Record<string, unknown>).input ?? null : null;

    // Write span_start immediately (before any sidecar calls)
    if (traceCfg.trace_dir) {
      const spanStart: SpanStartRecord = {
        schema_version: TRACE_SCHEMA_VERSION,
        record_type: "span_start",
        trace_id: traceId,
        span_id: spanId,
        parent_span_id: parentSpanId,
        session_id: sessionId,
        run_id: runId,
        agent_id: agentId,
        sequence_no: registry?.getSpan(traceId, spanId)?.sequenceNo ?? 0,
        kind: "tool",
        name: toolName,
        wall_time_ns: startWall.toString(),
        monotonic_time_ns: startMono.toString(),
        input: {
          requested_args: traceCfg.redact_sensitive_data
            ? (sanitizeTraceData(hookParams) as Record<string, unknown> | null)
            : (jsonSafe(hookParams) as Record<string, unknown> | null),
        },
        execution: {
          mode: null, // Will be filled by instrumentExecParams
          execution_id: null,
        },
        correlation: correlationStatus === "unresolved" ? {
          status: correlationStatus,
          reason: correlationReason,
        } : undefined,
      };
      const w = await getRunWriter(traceCfg.trace_dir, runId, sessionId, agentId, logger, traceCfg.flush_span_start);
      if (w) {
        w.writeRecord(spanStart);
        if (registry) registry.markStartWritten(traceId, spanId);
      }
    }

    // Original sidecar logic
    const payload = buildToolBefore(event, config);
    mergeContext(payload, context);
    payload.resource_scope = buildTrustedResourceScope(event, context) ?? buildRuntimeResourceScope(toolName);
    try {
      const decision = await client.decide(payload);
      if (config.mode === "enforce" && decision.action === "block") {
        return {
          block: true,
          blockReason: decision.reason
        };
      }
      const instrumentation = await instrumentExecParams(event, context, payload, decision, client, config);
      correlation.set(payload.tool_call_id, decision.decision_id, decision.lease_id, instrumentation.executionId);

      // Store command variants for span_end
      if (registry) {
        const span = registry.getSpan(traceId, spanId);
        if (span) {
          span.metadata = {
            requestedCommand: instrumentation.requestedCommand,
            effectiveCommand: instrumentation.effectiveCommand,
            payloadCommand: instrumentation.payloadCommand,
            executionId: instrumentation.executionId,
          };
        }
      }

      const sandboxParams = normalizeSandboxToolParams(
        instrumentation.params ?? cloneEventParams(event),
        toolName
      );
      if (instrumentation.params !== null) {
        return {params: sandboxParams.params ?? instrumentation.params};
      }
      return sandboxParams.changed && sandboxParams.params !== null ? {params: sandboxParams.params} : undefined;
    } catch (error) {
      logger.warn("Agent Scheduler decision failed", classifyError(error));
      const sandboxParams = normalizeSandboxToolParams(cloneEventParams(event), toolName);
      if ((config.mode === "observe" || config.failOpen) && sandboxParams.changed && sandboxParams.params !== null) {
        return {params: sandboxParams.params};
      }
      if (config.mode === "observe" || config.failOpen) return undefined;
      return {
        block: true,
        blockReason: "Agent Scheduler sidecar unavailable and failOpen=false."
      };
    }
  });

  // ── after_tool_call ───────────────────────────────────────────────

  api.on("after_tool_call", async (event: unknown, context: unknown) => {
    dumpHookShape(event, context, "after_tool_call");
    const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
    const toolCallId = extractString(event, ["tool_call_id", "toolCallId", "id"]);
    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const runId = extractString(event, ["run_id", "runId"]) ?? extractString(context, ["runId", "run_id"]);
    const traceId = runId ?? extractString(event, ["session_id", "sessionId"]) ?? extractString(context, ["sessionId", "session_id"]) ?? "unknown-run";
    const spanId = toolCallId ?? traceId;

    const endWall = wallClockNowNs();
    const endMono = monotonicNowNs();

    // Look up the active span for start times
    const activeSpan = registry?.endSpan(traceId, spanId) ?? null;

    // Clean up parent mapping
    if (toolCallId && registry) {
      registry.clearToolCallParent(toolCallId);
    }
    // Clean up pending tool name mapping
    if (toolCallId) pendingToolNames.delete(toolCallId);

    const startMono = activeSpan?.startMonotonicTimeNs ?? endMono;
    const startWall = activeSpan?.startWallTimeNs ?? endWall;
    const parentSpanId = activeSpan?.parentSpanId ?? null;
    const durNs = durationNs(startMono, endMono);

    // Original sidecar logic
    const completion = buildCompletion(
      event,
      correlation.take(extractString(event, ["tool_call_id", "toolCallId", "id"])),
      config
    );
    mergeContext(completion, context);
    completion.resource_scope = buildTrustedResourceScope(event, context) ?? buildRuntimeResourceScope(toolName);
    if (completion.resource_scope === null && completion.execution_id !== null) {
      try {
        completion.resource_scope = await client.getExecutionScope(completion.execution_id);
      } catch (error) {
        logger.warn("Agent Scheduler execution scope lookup failed", classifyError(error));
      }
    }
    let toolResourceTelemetry: unknown | null = null;
    try {
      await client.reportCompletion(completion);
    } catch (error) {
      logger.warn("Agent Scheduler completion report failed", classifyError(error));
    }
    if (completion.execution_id !== null) {
      try {
        toolResourceTelemetry = await client.getExecutionTelemetry(completion.execution_id);
      } catch (error) {
        logger.warn("Agent Scheduler execution telemetry lookup failed", classifyError(error));
      }
    }

    // Determine status code
    const toolExitCode = extractToolExitCode(completion.raw_result, completion.tool_name);
    const toolSucceeded = completion.succeeded && (toolExitCode === null || toolExitCode === 0);

    // ── verbose console: tool result ──
    const durMs = completion.duration_ms ?? Math.round(Number(durNs) / 1e6);
    const exitStr = toolExitCode !== null ? ` exit=${toolExitCode}` : "";
    const statusStr = completion.succeeded ? "ok" : (completion.error_type ?? "failed");
    const resultStr = summarizeToolResult(event);
    consoleVerbose(`■ ${toolName} done (${durMs}ms) ${statusStr}${exitStr}${resultStr ? ` | ${resultStr}` : ""}`);

    let statusCode: StatusCode = "unknown";
    if (toolSucceeded) {
      statusCode = "ok";
    } else if (completion.error_type === "timeout") {
      statusCode = "timeout";
    } else if (completion.error_type === "cancelled") {
      statusCode = "cancelled";
    } else if (completion.error_type || toolExitCode !== null || completion.succeeded === false) {
      statusCode = "error";
    }

    // Build execution info
    const execMode: ExecutionMode = completion.execution_id
      ? (config.executionBackend === "managed-wrapper" ? "launcher" : "marker")
      : "in_process_or_runtime_managed";

    const scope = completion.resource_scope;
    const meta = activeSpan?.metadata;
    const execInfo: SpanEndExecution = {
      mode: execMode,
      execution_id: completion.execution_id,
      requested_command: (meta?.requestedCommand as string | null) ?? null,
      effective_command: (meta?.effectiveCommand as string | null) ?? null,
      payload_command: (meta?.payloadCommand as string | null) ?? null,
      payload_pid: scope?.root_pid ?? scope?.pid ?? null,
      payload_pid_start_time_ticks: scope?.root_starttime_ticks ?? null,
      cgroup_path: scope?.cgroup_path ?? null,
      cgroup_id: null,
      pid_role: scope?.root_pid ? "payload_root" : (scope?.pid ? "payload_root" : null),
      tool_resource: toolResourceTelemetry,
    };

    // Sanitize command fields for trace
    if (traceCfg.redact_sensitive_data) {
      if (execInfo.effective_command) {
        execInfo.effective_command = sanitizeTraceData(execInfo.effective_command) as string;
      }
    }

    // Build resource info
    const resourceScope = completion.resource_scope;
    const hasPid = (resourceScope?.pid ?? resourceScope?.root_pid) != null;
    const hasCgroup = resourceScope?.cgroup_path != null;
    const isSharedRuntime = isSharedRuntimeScope(resourceScope);

    let attrStatus: AttributionStatus;
    let resQuality: MonitorQuality = "unknown";
    let coverageReason: CoverageReason | string = "pid_unavailable";

    if (!hasPid && !hasCgroup) {
      attrStatus = "unattributed";
      coverageReason = completion.execution_id ? "pid_unavailable" : "internal_tool_no_process";
    } else if (hasCgroup) {
      attrStatus = "attributed";
      coverageReason = "full_window";
      resQuality = "partial"; // We don't know the exact monitor window without launcher data
    } else if (isSharedRuntime) {
      attrStatus = "partially_attributed";
      coverageReason = "shared_runtime_process";
      resQuality = "partial";
    } else {
      attrStatus = "partially_attributed";
      coverageReason = "pid_registered_late";
      resQuality = "partial";
    }

    // For native tools with no PID, mark appropriately
    if (!completion.execution_id && !hasPid) {
      attrStatus = "unattributed";
      resQuality = "unknown";
      coverageReason = "internal_tool_no_process";
    }

    const resources: SpanEndResources = {
      attribution_status: attrStatus,
      scope: hasCgroup ? "cgroup" : (hasPid ? "process_tree" : "none"),
      quality: resQuality,
      monitor_start_wall_time_ns: null,
      monitor_end_wall_time_ns: null,
      monitor_start_monotonic_ns: null,
      monitor_end_monotonic_ns: null,
      coverage_duration_ns: null,
      action_duration_ns: durNs.toString(),
      coverage_ratio: null,
      coverage_reason: coverageReason,
      cpu_time_s: null,
      rss_peak_bytes: null,
    };

    // Write span_end
    if (traceCfg.trace_dir) {
      const seqNo = activeSpan?.sequenceNo ?? 0;
      const traceExitCode = traceExitCodeForTool(toolName, statusCode, toolExitCode);
      // Prefer span values; fall back to event/context for session_id, agent_id
      const finalSessionId = activeSpan?.sessionId ?? extractString(event, ["session_id", "sessionId"]) ?? extractString(context, ["sessionId", "session_id"]);
      const finalAgentId = activeSpan?.agentId ?? extractString(event, ["agent_id", "agentId"]) ?? extractString(context, ["agentId", "agent_id"]);
      const spanEnd: SpanEndRecord = {
        schema_version: TRACE_SCHEMA_VERSION,
        record_type: "span_end",
        trace_id: traceId,
        span_id: spanId,
        parent_span_id: parentSpanId,
        session_id: finalSessionId,
        run_id: runId,
        agent_id: finalAgentId,
        sequence_no: seqNo,
        kind: "tool",
        name: toolName,
        wall_time_ns: endWall.toString(),
        monotonic_time_ns: endMono.toString(),
        duration_ns: durNs.toString(),
        duration_sec: (Number(durNs) / 1e9).toString(),
        observed_duration_ms: completion.duration_ms ?? null,
        status: {
          code: statusCode,
          message: completion.error_type ?? (toolExitCode !== null && toolExitCode !== 0 ? `exit_code_${toolExitCode}` : null),
        },
        output: {
          exit_code: traceExitCode,
          result: traceCfg.include_tool_outputs
            ? (traceCfg.redact_sensitive_data
                ? sanitizeTraceData(completion.raw_result)
                : completion.raw_result)
            : null,
        },
        execution: execInfo,
        resources,
        correlation: activeSpan === null ? {
          status: "unresolved",
          reason: "span_start_not_found",
        } : undefined,
      };
      const w = await getRunWriter(traceCfg.trace_dir, runId, finalSessionId, finalAgentId, logger, traceCfg.flush_span_start);
      if (w) w.writeRecord(spanEnd);
    }
  });

  // ── model_call_started ────────────────────────────────────────────

  let llmSeqCounter = 0;

  api.on("model_call_started", async (event: unknown, context: unknown) => {
    dumpHookShape(event, context, "model_call_started");
    const callId = extractString(event, ["call_id", "callId", "id"]);
    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const runId = extractString(event, ["run_id", "runId"]) ?? extractString(context, ["runId", "run_id"]);
    const sessionId = extractString(event, ["session_id", "sessionId"]) ?? extractString(context, ["sessionId", "session_id"]);
    const agentId = extractString(event, ["agent_id", "agentId"]) ?? extractString(context, ["agentId", "agent_id"]);
    const model = extractString(event, ["model"]) ?? "unknown-model";
    const provider = extractString(event, ["provider"]);
    const traceId = runId ?? sessionId ?? "unknown-run";

    // ── verbose console: turn start ──
    turnCounter++;
    const inputMessages = extractModelInput(event);
    consoleVerbose(`── Turn ${turnCounter} ── model: ${model} | input: ${summarizeMessages(inputMessages)}`);

    llmSeqCounter++;
    const spanId = callId ?? `${traceId}:model:${llmSeqCounter}`;

    const startWall = wallClockNowNs();
    const startMono = monotonicNowNs();

    if (registry) {
      registry.beginSpan({
        traceId,
        spanId,
        parentSpanId: null, // Top-level LLM calls have no parent
        sessionId,
        runId,
        agentId,
        kind: "llm",
        name: model,
        startWallTimeNs: startWall,
        startMonotonicTimeNs: startMono,
      });
    }

    // Write span_start for LLM
    if (traceCfg.trace_dir) {
      const hookInput = extractModelInput(event);
      const spanStart: SpanStartRecord = {
        schema_version: TRACE_SCHEMA_VERSION,
        record_type: "span_start",
        trace_id: traceId,
        span_id: spanId,
        parent_span_id: null,
        session_id: sessionId,
        run_id: runId,
        agent_id: agentId,
        sequence_no: registry?.getSpan(traceId, spanId)?.sequenceNo ?? 0,
        kind: "llm",
        name: model,
        wall_time_ns: startWall.toString(),
        monotonic_time_ns: startMono.toString(),
        input: {
          requested_args: null,
          messages: traceCfg.include_llm_messages
            ? (traceCfg.redact_sensitive_data
                ? (sanitizeTraceData(hookInput) as unknown[] | null)
                : (jsonSafe(hookInput) as unknown[] | null))
            : null,
        },
        execution: {
          mode: null,
          execution_id: null,
        },
      };
      const w = await getRunWriter(traceCfg.trace_dir, runId, sessionId, agentId, logger, traceCfg.flush_span_start);
      if (w) {
        w.writeRecord(spanStart);
        if (registry) registry.markStartWritten(traceId, spanId);
      }
    }

    // Original sidecar logic
    await reportModel(client, logger, event, "model_call_started", config);

    // Store call_id -> span_id mapping for model_call_ended
    if (callId && registry) {
      // Store in a side map (we can use tool_call_parent with a special prefix)
      registry.setToolCallParent(`__llm_call__${callId}`, spanId);
    }
  });

  // ── model_call_ended ──────────────────────────────────────────────

  api.on("model_call_ended", async (event: unknown, context: unknown) => {
    dumpHookShape(event, context, "model_call_ended");
    const callId = extractString(event, ["call_id", "callId", "id"]);
    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const runId = extractString(event, ["run_id", "runId"]) ?? extractString(context, ["runId", "run_id"]);
    const traceId = runId ?? extractString(event, ["session_id", "sessionId"]) ?? extractString(context, ["sessionId", "session_id"]) ?? "unknown-run";

    // Look up the span_id from the started event
    let spanId = callId ?? "";
    if (callId && registry) {
      const mappedSpanId = registry.getToolCallParent(`__llm_call__${callId}`);
      if (mappedSpanId) {
        spanId = mappedSpanId;
        registry.clearToolCallParent(`__llm_call__${callId}`);
      }
    }
    if (!spanId) {
      llmSeqCounter++;
      spanId = `${traceId}:model:${llmSeqCounter}`;
    }

    const endWall = wallClockNowNs();
    const endMono = monotonicNowNs();

    const activeSpan = registry?.endSpan(traceId, spanId) ?? null;
    const startMono = activeSpan?.startMonotonicTimeNs ?? endMono;
    const durNs = durationNs(startMono, endMono);

    const model = extractString(event, ["model"]) ?? activeSpan?.name ?? "unknown-model";
    const outcome = extractString(event, ["outcome", "status"]);
    const durationMs = extractNumber(event, ["duration_ms", "durationMs"]);

    // ── verbose console: model response ──
    const modelOutput = extractModelOutput(event);
    const textContent = extractTextContent(modelOutput);
    const displayToolCalls = extractToolCallsForDisplay(modelOutput);
    if (textContent) {
      consoleVerbose(`→ ${truncateStr(textContent, 500)}`);
    }
    for (const tc of displayToolCalls) {
      consoleVerbose(`→ tool: ${tc.name}`);
    }

    // Extract tool calls from the response to set up parent mapping
    if (registry) {
      const toolCalls = extractToolCallsFromResponse(event);
      for (const tcId of toolCalls) {
        registry.setToolCallParent(tcId, spanId);
      }
    }

    // Determine status
    let statusCode: StatusCode = "unknown";
    if (outcome === "completed" || outcome === "ok" || outcome === "success") {
      statusCode = "ok";
    } else if (outcome === "error" || outcome === "failed") {
      statusCode = "error";
    } else if (outcome === "timeout") {
      statusCode = "timeout";
    } else if (outcome === "cancelled") {
      statusCode = "cancelled";
    }

    // Write span_end for LLM
    if (traceCfg.trace_dir) {
      const hookOutput = extractModelOutput(event);
      // Prefer span values; fall back to event/context for session_id, agent_id
      const finalSessionId = activeSpan?.sessionId ?? extractString(event, ["session_id", "sessionId"]) ?? extractString(context, ["sessionId", "session_id"]);
      const finalAgentId = activeSpan?.agentId ?? extractString(event, ["agent_id", "agentId"]) ?? extractString(context, ["agentId", "agent_id"]);
      const spanEnd: SpanEndRecord = {
        schema_version: TRACE_SCHEMA_VERSION,
        record_type: "span_end",
        trace_id: traceId,
        span_id: spanId,
        parent_span_id: null,
        session_id: finalSessionId,
        run_id: runId,
        agent_id: finalAgentId,
        sequence_no: activeSpan?.sequenceNo ?? 0,
        kind: "llm",
        name: model,
        wall_time_ns: endWall.toString(),
        monotonic_time_ns: endMono.toString(),
        duration_ns: durNs.toString(),
        duration_sec: (Number(durNs) / 1e9).toString(),
        observed_duration_ms: durationMs ?? null,
        status: {
          code: statusCode,
          message: null,
        },
        output: {
          content: traceCfg.include_tool_outputs
            ? (traceCfg.redact_sensitive_data
                ? sanitizeTraceData(hookOutput)
                : jsonSafe(hookOutput))
            : null,
        },
        execution: {
          mode: null,
          execution_id: null,
        },
        resources: {
          attribution_status: "not_applicable",
          scope: "none",
          quality: "unknown",
          monitor_start_wall_time_ns: null,
          monitor_end_wall_time_ns: null,
          monitor_start_monotonic_ns: null,
          monitor_end_monotonic_ns: null,
          coverage_duration_ns: null,
          action_duration_ns: durNs.toString(),
          coverage_ratio: null,
          coverage_reason: "not_applicable",
        },
        correlation: activeSpan === null ? {
          status: "unresolved",
          reason: "span_start_not_found",
        } : undefined,
      };
      const w = await getRunWriter(traceCfg.trace_dir, runId, finalSessionId, finalAgentId, logger, traceCfg.flush_span_start);
      if (w) w.writeRecord(spanEnd);
    }

    // Original sidecar logic
    await reportModel(client, logger, event, "model_call_ended", config);
  });

  // ── Shutdown handling ────────────────────────────────────────────────
  // Write interrupted spans when plugin is being unloaded
  process.on("beforeExit", async () => {
    if (registry && runWriters.size > 0) {
      const activeSpans = registry.listActiveSpans();
      const endWall = wallClockNowNs();
      const endMono = monotonicNowNs();

      for (const span of activeSpans) {
        const durNs = durationNs(span.startMonotonicTimeNs, endMono);
        const spanEnd: SpanEndRecord = {
          schema_version: TRACE_SCHEMA_VERSION,
          record_type: "span_end",
          trace_id: span.traceId,
          span_id: span.spanId,
          parent_span_id: span.parentSpanId,
          session_id: span.sessionId,
          run_id: span.runId,
          agent_id: span.agentId,
          sequence_no: span.sequenceNo,
          kind: span.kind,
          name: span.name,
          wall_time_ns: endWall.toString(),
          monotonic_time_ns: endMono.toString(),
          duration_ns: durNs.toString(),
          duration_sec: (Number(durNs) / 1e9).toString(),
          status: {
            code: "interrupted",
            message: "plugin shutdown before span completion",
          },
          output: {},
          execution: {
            mode: null,
            execution_id: null,
          },
          resources: {
            attribution_status: "not_applicable",
            scope: "none",
            quality: "unknown",
            monitor_start_wall_time_ns: null,
            monitor_end_wall_time_ns: null,
            monitor_start_monotonic_ns: null,
            monitor_end_monotonic_ns: null,
            coverage_duration_ns: null,
            action_duration_ns: durNs.toString(),
            coverage_ratio: null,
            coverage_reason: "not_applicable",
          },
        };
        // Write to the span's run writer
        const w = runWriters.get(span.runId ?? span.traceId);
        if (w) w.writeRecord(spanEnd);
      }
      // Close all writers on shutdown
      for (const w of runWriters.values()) {
        await w.close();
      }
    }
  });
}
});

// ── Helper functions ───────────────────────────────────────────────────

function common(event: unknown): CommonEvent {
  const runtimeSessionKey = getRuntimeSessionKey();
  const sessionKey = extractString(event, ["session_key", "sessionKey"]) ?? runtimeSessionKey;
  return {
    schema_version: "scheduler.v1",
    event_id: randomUUID(),
    occurred_at: new Date().toISOString(),
    plugin_version: pluginVersion,
    run_id: extractString(event, ["run_id", "runId"]) ?? getRuntimeRunId(),
    session_id: extractString(event, ["session_id", "sessionId"]) ?? sessionKey,
    session_key: sessionKey,
    agent_id: extractString(event, ["agent_id", "agentId"]) ?? getRuntimeAgentId()
  };
}

function buildToolBefore(event: unknown, config: PluginConfig): ToolBeforeRequest {
  const params = isRecord(event) ? (event as Record<string, unknown>).params ?? (event as Record<string, unknown>).arguments ?? (event as Record<string, unknown>).input ?? null : null;
  const safeParams = redact(params);
  const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
  const includeRaw = config.trace?.include_raw_events === true;
  const rawEvent = includeRaw && isRecord(event)
    ? (config.trace.redact_sensitive_data ? redact(event) : jsonSafe(event))
    : null;
  return {
    ...common(event),
    tool_call_id: extractString(event, ["tool_call_id", "toolCallId", "id"]),
    tool_name: toolName,
    tool_kind: extractString(event, ["tool_kind", "toolKind", "kind"]),
    tool_input_kind: extractString(event, ["tool_input_kind", "toolInputKind", "inputKind"]),
    operation_hint: null,
    derived_paths: [],
    params_digest: stableDigest(safeParams),
    param_features: paramFeatures(safeParams),
    raw_params: jsonSafe(safeParams),
    raw_event: rawEvent,
    resource_scope: null
  };
}

function mergeContext(payload: CommonEvent, context: unknown): void {
  const runtimeSessionKey = getRuntimeSessionKey();
  payload.run_id = payload.run_id ?? extractString(context, ["runId", "run_id"]) ?? getRuntimeRunId();
  payload.session_key = payload.session_key ?? extractString(context, ["sessionKey", "session_key"]) ?? runtimeSessionKey;
  payload.session_id = payload.session_id ?? extractString(context, ["sessionId", "session_id"]) ?? payload.session_key;
  payload.agent_id = payload.agent_id ?? extractString(context, ["agentId", "agent_id"]) ?? getRuntimeAgentId();
}

function buildCompletion(
  event: unknown,
  prior: {decisionId: string | null; leaseId: string | null; executionId: string | null} | null,
  config: PluginConfig
): ToolCompletedEvent {
  const errorType = extractString(event, ["error_type", "errorType"]);
  const includeOutput = config.trace?.include_tool_outputs !== false; // default true
  const includeRaw = config.trace?.include_raw_events === true;
  const rawResult = includeOutput && isRecord(event)
    ? (event as Record<string, unknown>).result ?? (event as Record<string, unknown>).output ?? (event as Record<string, unknown>).response ?? null
    : null;
  const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
  const exitCode = extractToolExitCode(rawResult, toolName);
  const explicitSucceeded = extractBoolean(event, ["succeeded", "success"]);
  const rawEvent = includeRaw && isRecord(event)
    ? (config.trace.redact_sensitive_data ? redact(event) : jsonSafe(event))
    : null;
  return {
    ...common(event),
    tool_call_id: extractString(event, ["tool_call_id", "toolCallId", "id"]),
    decision_id: prior?.decisionId ?? null,
    lease_id: prior?.leaseId ?? null,
    execution_id: prior?.executionId ?? null,
    tool_name: toolName,
    duration_ms: extractNumber(event, ["duration_ms", "durationMs"]) ?? 0,
    succeeded: exitCode === 0 ? true : (explicitSucceeded ?? (errorType === null && exitCode === null)),
    error_type: errorType,
    error_digest: null,
    result_size_bytes: extractNumber(event, ["result_size_bytes", "resultSizeBytes"]),
    raw_result: includeOutput ? jsonSafe(rawResult) : null,
    raw_event: rawEvent,
    resource_scope: null
  };
}

function buildRuntimeResourceScope(toolName: string): ResourceScope | null {
  if (toolName === "exec") return null;
  if (typeof process.pid !== "number" || process.pid <= 0) return null;
  const processStartTime = Math.max(0, Date.now() / 1000 - process.uptime());
  const cgroupPath = readSelfCgroupPath();
  return {
    kind: cgroupPath === null ? "pid" : "cgroup-v2",
    execution_id: null,
    pid: process.pid,
    root_pid: process.pid,
    process_start_time: processStartTime,
    root_starttime_ticks: null,
    cgroup_path: cgroupPath,
    pid_namespace_inode: null,
    container_id: null,
    include_children: true,
    source: "openclaw-runtime",
    attribution_source: "shared-runtime-process",
  };
}

function cloneEventParams(event: unknown): Record<string, unknown> | null {
  return cloneToolParams(isRecord(event) ? event.params ?? event.arguments ?? event.input ?? null : null);
}

function cloneToolParams(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
}

function readSelfCgroupPath(): string | null {
  if (process.platform === "win32") return null;
  try {
    const text = readFileSync("/proc/self/cgroup", "utf8");
    for (const rawLine of text.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line.startsWith("0::")) continue;
      const path = line.slice(3);
      if (!path || path === "/") return "/sys/fs/cgroup";
      return `/sys/fs/cgroup${path}`;
    }
  } catch {
    return null;
  }
  return null;
}

function isSharedRuntimeScope(scope: ResourceScope | null): boolean {
  return scope?.source === "openclaw-runtime" || scope?.attribution_source === "shared-runtime-process";
}

async function reportModel(
  client: SidecarClient,
  logger: {warn(message: string, data?: unknown): void},
  event: unknown,
  eventType: "model_call_started" | "model_call_ended",
  config: PluginConfig
): Promise<void> {
  try {
    const includeRaw = config.trace?.include_raw_events === true;
    const rawEvent = includeRaw && isRecord(event)
      ? (config.trace.redact_sensitive_data ? redact(event) : jsonSafe(event))
      : null;
    const payload: ModelEvent = {
      ...common(event),
      event_type: eventType,
      call_id: extractString(event, ["call_id", "callId", "id"]),
      provider: extractString(event, ["provider"]),
      model: extractString(event, ["model"]),
      duration_ms: extractNumber(event, ["duration_ms", "durationMs"]),
      outcome: extractString(event, ["outcome", "status"]),
      context_token_budget: extractNumber(event, ["context_token_budget", "contextTokenBudget"]),
      raw_input: eventType === "model_call_started" && config.trace.include_llm_messages
        ? jsonSafe(config.trace.redact_sensitive_data ? sanitizeTraceData(extractModelInput(event)) : extractModelInput(event))
        : null,
      raw_output: eventType === "model_call_ended" && config.trace.include_tool_outputs
        ? jsonSafe(config.trace.redact_sensitive_data ? sanitizeTraceData(extractModelOutput(event)) : extractModelOutput(event))
        : null,
      raw_event: rawEvent
    };
    await client.reportModel(payload);
  } catch (error) {
    logger.warn("Agent Scheduler model report failed", classifyError(error));
  }
}

/**
 * Extract tool call IDs from a model_call_ended event's response.
 * Handles various shapes: choices[].message.tool_calls, tool_calls, etc.
 */
function extractToolCallsFromResponse(event: unknown): string[] {
  if (!isRecord(event)) return [];
  const evt = event as Record<string, unknown>;

  // Try various paths to find tool calls
  const output = evt.output ?? evt.response ?? evt.content ?? evt.message ?? evt.choices ?? evt;

  if (!isRecord(output)) return [];

  // Direct tool_calls array
  const directCalls = (output as Record<string, unknown>).tool_calls;
  if (Array.isArray(directCalls)) {
    return directCalls.map((tc: unknown) => {
      if (isRecord(tc)) return (tc as Record<string, unknown>).id ?? (tc as Record<string, unknown>).tool_call_id;
      return null;
    }).filter((id: unknown): id is string => typeof id === "string");
  }

  // choices[0].message.tool_calls (OpenAI-style)
  const choices = (output as Record<string, unknown>).choices;
  if (Array.isArray(choices) && choices.length > 0 && isRecord(choices[0])) {
    const message = (choices[0] as Record<string, unknown>).message ?? (choices[0] as Record<string, unknown>).delta;
    if (isRecord(message)) {
      const toolCalls = (message as Record<string, unknown>).tool_calls;
      if (Array.isArray(toolCalls)) {
        return toolCalls.map((tc: unknown) => {
          if (isRecord(tc)) return (tc as Record<string, unknown>).id ?? (tc as Record<string, unknown>).tool_call_id;
          return null;
        }).filter((id: unknown): id is string => typeof id === "string");
      }
    }
  }

  return [];
}

function extractModelInput(event: unknown): unknown {
  if (!isRecord(event)) return null;
  const evt = event as Record<string, unknown>;
  return evt.messages ?? evt.input ?? evt.prompt ?? evt.request ?? evt.body ?? null;
}

function extractModelOutput(event: unknown): unknown {
  if (!isRecord(event)) return null;
  const evt = event as Record<string, unknown>;
  return evt.output ?? evt.response ?? evt.content ?? evt.message ?? evt.choices ?? evt.body ?? null;
}

function extractString(value: unknown, keys: string[]): string | null {
  if (!isRecord(value)) return null;
  const rec = value as Record<string, unknown>;
  for (const key of keys) {
    const item = rec[key];
    if (typeof item === "string" && item.length > 0) return item;
  }
  return null;
}

function getRuntimeSessionKey(): string | null {
  return argvValue("--session-key");
}

function getRuntimeRunId(): string | null {
  return firstEnvString([
    "OPENCLAW_RUN_ID",
    "OPENCLAW_AGENT_RUN_ID",
    "CLAW_RUN_ID",
  ]) ?? argvValue("--run-id");
}

function getRuntimeAgentId(): string | null {
  return firstEnvString([
    "OPENCLAW_AGENT_ID",
    "CLAW_AGENT_ID",
  ]) ?? argvValue("--agent");
}

function firstEnvString(keys: string[]): string | null {
  for (const key of keys) {
    const value = process.env[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

function argvValue(name: string): string | null {
  for (let index = 0; index < process.argv.length; index += 1) {
    const arg = process.argv[index];
    if (arg === name) {
      const next = process.argv[index + 1];
      return typeof next === "string" && next.length > 0 ? next : null;
    }
    const prefix = name + "=";
    if (arg.startsWith(prefix) && arg.length > prefix.length) {
      return arg.slice(prefix.length);
    }
  }
  return null;
}

function extractNumber(value: unknown, keys: string[]): number | null {
  if (!isRecord(value)) return null;
  const rec = value as Record<string, unknown>;
  for (const key of keys) {
    const item = rec[key];
    if (typeof item === "number" && Number.isFinite(item) && item >= 0) return Math.floor(item);
  }
  return null;
}

function extractBoolean(value: unknown, keys: string[]): boolean | null {
  if (!isRecord(value)) return null;
  const rec = value as Record<string, unknown>;
  for (const key of keys) {
    const item = rec[key];
    if (typeof item === "boolean") return item;
  }
  return null;
}

function classifyError(error: unknown): {type: string; message: string} {
  if (error instanceof Error) return {type: error.name, message: error.message};
  return {type: "unknown", message: String(error)};
}
