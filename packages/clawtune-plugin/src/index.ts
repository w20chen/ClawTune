import {definePluginEntry, type HookApi} from "openclaw/plugin-sdk/plugin-entry";
import {randomUUID} from "node:crypto";
import {readFileSync} from "node:fs";
import {SidecarClient} from "./client.js";
import {loadConfig, isRecord} from "./config.js";
import {CorrelationMap} from "./correlation.js";
import type {CommonEvent, ModelEvent, PluginConfig, SidecarHealth, ToolBeforeRequest, ToolCompletedEvent, ToolDecision} from "./contracts.js";
import {MIN_COMPATIBLE_SIDECAR_VERSION, REQUIRED_PROTOCOL_VERSIONS} from "./contracts.js";
import type {ResourceScope} from "./contracts.js";
import {buildTrustedResourceScope, instrumentExecParams} from "./exec-instrumentation.js";
import type {InstrumentResult} from "./exec-instrumentation.js";
import {consoleLogger} from "./logging.js";
import {jsonSafe, paramFeatures, redact, stableDigest} from "./redaction.js";
import {resolveRepoKey} from "./repo.js";
import {normalizeSandboxToolParams} from "./sandbox-paths.js";
import {ensureSidecarRunning, type SidecarLauncherResult} from "./sidecar-launcher.js";
import {
  SpanRegistry,
} from "./trace/registry.js";
import {RunWriterManager, type RunWriterScope} from "./trace/run-writer-manager.js";
import {
  monotonicNowNs,
  wallClockNowNs,
  durationNs,
} from "./trace/clock.js";
import {
  sanitizeTraceData,
} from "./trace/sanitizer.js";
import {extractToolExitCode, traceExitCodeForTool} from "./tool-result.js";
import type {
  SpanStartRecord,
  SpanEndRecord,
  SpanKind,
  StatusCode,
  ExecutionMode,
  AttributionStatus,
  MonitorQuality,
  CoverageReason,
  SpanEndExecution,
  SpanEndResources,
  ActiveSpan,
} from "./trace/schema.js";
import { TRACE_SCHEMA_VERSION } from "./trace/schema.js";

const pluginVersion = "0.1.0";

// ── Plugin-wide state ──────────────────────────────────────────────────

/** Unique instance ID generated once per plugin load (�?per CLI launch). */
const instanceId = randomUUID();

export default definePluginEntry({
  id: "clawtune",
  name: "ClawTune",
  description: "Hardware-aware tracing and scheduling for OpenClaw.",
  configSchema: {
    type: "object",
    additionalProperties: false,
    properties: {
      endpoint: {type: "string", default: "http://localhost:8765"},
      mode: {enum: ["observe", "enforce"], default: "observe"},
      decisionTimeoutMs: {type: "integer", default: 3000, minimum: 200, maximum: 30000},
      reportTimeoutMs: {type: "integer", default: 3000, minimum: 200, maximum: 30000},
      failOpen: {type: "boolean", default: true},
      logLevel: {enum: ["error", "warn", "info", "debug"], default: "info"},
      consoleMode: {enum: ["verbose", "quiet"], default: "verbose"},
      executionBackend: {enum: ["hook-only", "marker", "managed-wrapper"], default: "managed-wrapper"},
      launcherPath: {type: "string", default: "", description: "Absolute path to clawtune-launch. Empty = auto-resolve from PATH."},
      launcherInterpreter: {type: ["string", "null"], default: null},
      collectorSocket: {type: "string", default: "/run/clawtune/collector.sock"},
      instrumentHosts: {type: "array", items: {type: "string"}, default: ["gateway", "*"]},
      instrumentTools: {type: "array", items: {type: "string"}, default: ["exec"]},
      enableCgroup: {type: "boolean", default: true},
      enableAffinity: {type: "boolean", default: true},
      enableNuma: {type: "boolean", default: true},
      profilingMode: {enum: ["off", "proc", "perf", "ksys", "vtune"], default: "off"},
      securityBoundaryAccepted: {type: "boolean", default: true},
      autoStartSidecar: {type: "boolean", default: false},
      sidecarStartupTimeoutMs: {
        type: "integer",
        default: 60_000,
        minimum: 1_000,
        maximum: 600_000,
      },
      sidecarCommand: {type: "string", default: ""},
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
  const runtimeId = process.env.CLAWTUNE_RUNTIME_ID?.trim() || randomUUID();
  const gatewayId = process.env.CLAWTUNE_GATEWAY_ID?.trim() || runtimeId;
  // KB repo namespace: CLAWTUNE_REPO_KEY env wins (swe-rebench sets it per task),
  // then plugin config `repo`, then git/working-directory auto-derivation.
  const runtimeRepo = resolveRepoKey(config.repo);
  if (runtimeRepo) {
    logger.info?.("KB repo namespace", {repo: runtimeRepo});
  }

  // ── Auto-start sidecar ───────────────────────────────────────────
  let sidecarLauncher: SidecarLauncherResult | null = null;
  /** Tracks the in-flight launch promise so beforeExit can await it. */
  let sidecarLaunchPromise: Promise<void> | null = null;
  let sidecarLaunchError: unknown | null = null;
  if (config.autoStartSidecar) {
    const launchPromise = ensureSidecarRunning({
      endpoint: config.endpoint,
      command: config.sidecarCommand,
      healthPollMs: 200,
      healthTimeoutMs: config.sidecarStartupTimeoutMs,
      logger: {
        info: (msg, data) => logger.info?.(msg, data),
        warn: (msg, data) => logger.warn?.(msg, data),
        error: (msg, data) => logger.error?.(msg, data),
      },
    });
    // Fire-and-forget the auto-start; if it fails the existing
    // failOpen path will handle the missing sidecar gracefully.
    // Track the promise so shutdown can await it before cleanup.
    sidecarLaunchPromise = launchPromise.then((result) => {
      sidecarLauncher = result;
    }).catch((err) => {
      sidecarLaunchError = err;
      logger.warn("sidecar auto-start failed", {
        error: String(err),
      });
    });
  }

  async function waitForAutoStartedSidecar(): Promise<void> {
    if (sidecarLaunchPromise === null) return;
    await sidecarLaunchPromise;
    if (sidecarLaunchError !== null) {
      throw new Error(`required sidecar auto-start failed: ${String(sidecarLaunchError)}`);
    }
    // Verify the sidecar version is compatible with this plugin.
    await checkSidecarCompatibility(config, logger);
  }

  /**
   * Lightweight semver comparison: returns true when `version` >= `minimum`.
   * Handles simple ``major.minor.patch`` strings (prerelease tags are ignored).
   */
  function semverGte(version: string, minimum: string): boolean {
    const vParts = version.split(".").map(Number);
    const mParts = minimum.split(".").map(Number);
    for (let i = 0; i < Math.max(vParts.length, mParts.length); i++) {
      const v = vParts[i] ?? 0;
      const m = mParts[i] ?? 0;
      if (isNaN(v) || isNaN(m)) return false;
      if (v > m) return true;
      if (v < m) return false;
    }
    return true; // equal
  }

  /**
   * Check that the running sidecar meets the minimum version and protocol
   * requirements.  Logs warnings when failOpen is true; throws when false.
   */
  async function checkSidecarCompatibility(
    cfg: PluginConfig,
    log: {warn?: (msg: string, data?: unknown) => void; error?: (msg: string, data?: unknown) => void},
  ): Promise<void> {
    let health: SidecarHealth | null = null;
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(`${cfg.endpoint}/health/live`, {
          method: "GET",
          signal: controller.signal,
        });
        if (response.ok) {
          health = (await response.json()) as SidecarHealth;
        }
      } finally {
        clearTimeout(timer);
      }
    } catch {
      // Health endpoint unreachable — the sidecar may still be starting.
      // The failOpen path will handle this gracefully on the first request.
      return;
    }

    if (!health) return;

    const version = health.sidecar_version;
    if (version && !semverGte(version, MIN_COMPATIBLE_SIDECAR_VERSION)) {
      const msg = `sidecar version ${version} is older than minimum ${MIN_COMPATIBLE_SIDECAR_VERSION}`;
      if (cfg.failOpen) {
        log.warn?.(msg, {sidecarVersion: version, minVersion: MIN_COMPATIBLE_SIDECAR_VERSION});
      } else {
        throw new Error(msg);
      }
    }

    const protocols = health.protocol_versions ?? [];
    const missingProtocols = REQUIRED_PROTOCOL_VERSIONS.filter(p => !protocols.includes(p));
    if (missingProtocols.length > 0) {
      const msg = `sidecar is missing required protocol versions: ${missingProtocols.join(", ")}`;
      if (cfg.failOpen) {
        log.warn?.(msg, {sidecarProtocols: protocols, required: REQUIRED_PROTOCOL_VERSIONS});
      } else {
        throw new Error(msg);
      }
    }
  }

  // Use the awaited lifecycle gate instead of the observation-only
  // model_call_started hook. OpenClaw awaits it before the
  // first provider request, eliminating the startup race with the local proxy.
  api.on("before_agent_start", async () => {
    await waitForAutoStartedSidecar();
  }, {priority: 1000, timeoutMs: config.sidecarStartupTimeoutMs + 5_000});

  const client = new SidecarClient(config);
  const correlation = new CorrelationMap(300_000, 10_000);

  // Initialize trace v6 if trace_dir is configured
  const registry = new SpanRegistry();
  const traceCfg = config.trace;
  const writerManager = new RunWriterManager(traceCfg.flush_span_start, logger);

  function writerScope(
    identity: HookIdentity,
    sessionId: string | null = identity.sessionId,
    agentId: string | null = identity.agentId,
  ): RunWriterScope {
    return {
      traceDir: traceCfg.trace_dir,
      runtimeId: identity.runtimeId,
      runId: identity.runId,
      sessionId,
      agentId,
    };
  }

  // ── Console turn-by-turn logging (verbose mode) ──────────────────
  const CONSOLE_PREFIX = "[openclaw]";
  let turnCounter = 0;
  const pendingToolNames = new Map<string, string>(); // toolCallId -> toolName
  // Per-correlation sidecar/plugin round-trip timing (ns), measured
  // in before_tool_call and consumed in after_tool_call.
  const pendingToolOverhead = new Map<string, {decisionNs: bigint}>();

  function consoleVerbose(msg: string): void {
    if (config.consoleMode !== "verbose") return;
    console.log(`${CONSOLE_PREFIX} ${msg}`);
  }

  function truncateStr(s: string, maxLen: number): string {
    if (s.length <= maxLen) return s;
    return s.slice(0, maxLen) + `...<truncated ${s.length - maxLen} chars>`;
  }

  function formatMs(ms: number | null | undefined): string {
    if (ms === null || ms === undefined || !Number.isFinite(ms)) return "?";
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  }

  function formatNumber(value: unknown, digits = 2): string {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "?";
  }

  function oneLine(value: string): string {
    return value.replace(/[\r\n\t]+/g, " ").replace(/\s{2,}/g, " ").trim();
  }

  function formatProbabilityList(values: unknown): string | null {
    if (!Array.isArray(values) || values.length === 0) return null;
    return values
      .map((value, index) => `b${index}=${formatNumber(value, 2)}`)
      .join(", ");
  }

  function formatBucketRange(bucketId: unknown): string {
    if (typeof bucketId !== "number" || !Number.isInteger(bucketId) || bucketId < 0) {
      return "unknown range";
    }
    const boundaries = [100, 500, 2_000, 10_000];
    if (bucketId === 0) return "under 100ms";
    if (bucketId < boundaries.length) {
      return `${formatMs(boundaries[bucketId - 1])}–${formatMs(boundaries[bucketId])}`;
    }
    return "10s or more";
  }

  function formatEvidence(value: unknown): string {
    if (!isRecord(value)) return "no evidence";
    const scope = typeof value.scope === "string" ? value.scope : "unknown scope";
    const keyKind = typeof value.key_kind === "string" ? value.key_kind : "unknown match";
    const evidence = typeof value.evidence_count === "number" ? value.evidence_count : 0;
    return `${scope}, ${keyKind}, ${evidence} sample${evidence === 1 ? "" : "s"}`;
  }

  function formatPredictionSource(value: unknown): string {
    if (!isRecord(value)) return "no evidence";
    const scope = typeof value.scope === "string" ? value.scope : "unknown scope";
    const keyKind = typeof value.key_kind === "string" ? value.key_kind : "unknown match";
    const evidence = typeof value.evidence_count === "number" ? value.evidence_count : 0;
    return `${scope}, ${keyKind}, ${evidence} sample${evidence === 1 ? "" : "s"}`;
  }

  function continuousPredictionSummary(prediction: unknown, label: string, unit: string): string {
    if (!isRecord(prediction)) return `${label}=?`;
    const p90 = prediction.conditional_p90;
    const note = typeof prediction.note === "string" && prediction.note
      ? `; ${oneLine(prediction.note)}`
      : "";
    const source = formatPredictionSource(prediction);
    if (typeof p90 !== "number" || !Number.isFinite(p90)) {
      return `${label}: unavailable (${source}${note})`;
    }
    const value = unit === "ms"
      ? formatMs(p90)
      : `${formatNumber(p90, unit === "cores" ? 2 : 1)}${unit}`;
    return `${label}: ${value} (${source}${note})`;
  }

  function summarizePrediction(decision: ToolDecision): string {
    const prediction = decision.prediction;
    const lines: string[] = [
      "prediction",
      `  duration: typical ${formatMs(prediction.duration_p50_ms)}; p90 ${formatMs(prediction.duration_p90_ms)}`,
      `  resources: class=${prediction.resource_class}`,
    ];
    if (prediction.confidence !== null && prediction.confidence !== undefined) {
      lines[2] += `; confidence=${formatNumber(prediction.confidence * 100, 0)}%`;
    }
    const toolResource = prediction.tool_resource;
    if (!toolResource) return lines.join("\n");

    if (toolResource.prediction) {
      let bucketTag: string;
      let probsSuffix = "";
      if (toolResource.composed === true) {
        const total = typeof toolResource.composed_total_ms === "number" ? toolResource.composed_total_ms : 0;
        const count = toolResource.prediction.evidence_count;
        bucketTag = `composed; total ~${formatMs(total)}; ${count} clause${count === 1 ? "" : "s"}`;
      } else {
        const probs = formatProbabilityList(toolResource.prediction.probability_by_bucket);
        bucketTag = `${formatBucketRange(toolResource.prediction.bucket_id)}; ${formatEvidence(toolResource.prediction)}`;
        probsSuffix = probs ? `\n    probabilities: ${probs}` : "";
      }
      lines.push(
        `  latency bucket: #${toolResource.prediction.bucket_id} (${bucketTag})` + probsSuffix,
      );
    } else {
      lines.push(`  latency bucket: unavailable (${toolResource.unavailable_reason ?? "unknown reason"})`);
    }

    const clausePreds = toolResource.clause_predictions;
    if (Array.isArray(clausePreds) && clausePreds.length > 0) {
      lines.push("  clauses:");
      for (const cp of clausePreds) {
        if (!isRecord(cp)) {
          lines.push("    unknown clause");
          continue;
        }
        const bin = typeof cp.bin === "string" ? oneLine(cp.bin) : "?";
        if (isRecord(cp.prediction)) {
          const p = cp.prediction;
          const probs = formatProbabilityList(p.probability_by_bucket);
          lines.push(
            `    ${bin} → #${p.bucket_id} (${formatBucketRange(p.bucket_id)}; ${formatEvidence(p)})` +
            (probs ? ` [${probs}]` : ""),
          );
        } else {
          lines.push(`    ${bin} → unavailable (${cp.unavailable_reason ?? "unknown reason"})`);
        }
      }
    }

    const composition = toolResource.composition;
    if (Array.isArray(composition) && composition.length > 0) {
      lines.push("  composition:");
      for (const unit of composition) {
        if (!isRecord(unit)) continue;
        const kind = unit.kind === "pipeline" ? "|" : "serial";
        const bins = Array.isArray(unit.bins) ? unit.bins.join(" ") : "?";
        const dropped =
          Array.isArray(unit.dropped_viewer_bins) && unit.dropped_viewer_bins.length > 0
            ? ` (dropped viewer: ${unit.dropped_viewer_bins.join(", ")})`
            : "";
        const timeMs = typeof unit.time_ms === "number" ? unit.time_ms : 0;
        lines.push(`    ${kind} ${bins} ~${formatMs(timeMs)}${dropped}`);
      }
    }

    const continuous = toolResource.continuous_predictions ?? {};
    lines.push("  runtime p90:");
    lines.push(`    ${continuousPredictionSummary(continuous.latency_ms, "latency", "ms")}`);
    lines.push(`    ${continuousPredictionSummary(continuous.peak_cpu_cores, "cpu", "cores")}`);
    lines.push(`    ${continuousPredictionSummary(continuous.peak_memory_mb, "memory", "MB")}`);

    const latticePreds = toolResource.lattice_time_predictions;
    if (Array.isArray(latticePreds) && latticePreds.length > 0) {
      lines.push("  lattice estimates:");
      for (const lp of latticePreds) {
        if (!isRecord(lp)) {
          lines.push("    unknown clause");
          continue;
        }
        const bin = typeof lp.bin === "string" ? oneLine(lp.bin) : "?";
        const predictions = Array.isArray(lp.predictions) ? lp.predictions : [];
        const estimates = predictions.map((item) => {
          if (!isRecord(item)) return "unknown";
          const algorithm = typeof item.algorithm === "string" ? item.algorithm : "unknown";
          if (typeof item.prediction_ms === "number" && Number.isFinite(item.prediction_ms)) {
            const evidence = typeof item.evidence_count === "number" ? item.evidence_count : 0;
            const match = item.exact_match === true ? "exact" : item.exact_match === false ? "generalized" : "unknown match";
            return `${algorithm}=${formatMs(item.prediction_ms)} (${match}, ${evidence} sample${evidence === 1 ? "" : "s"})`;
          }
          return `${algorithm}=unavailable (${item.unavailable_reason ?? "unknown reason"})`;
        });
        lines.push(`    ${bin}: ${estimates.join("; ")}`);
      }
    }

    const enabled = toolResource.prediction_algorithms?.enabled
      ?.map((item) => item.name)
      .filter((name): name is string => typeof name === "string" && name.length > 0);
    if (enabled && enabled.length > 0) {
      lines.push(`  algorithms: ${enabled.join(", ")}`);
    }
    return lines.join("\n");
  }

  function extractTextContent(output: unknown): string | null {
    if (!isRecord(output)) return null;
    const choices = output.choices;
    if (Array.isArray(choices) && choices.length > 0 && isRecord(choices[0])) {
      const choice = choices[0];
      const message = choice.message ?? choice.delta;
      if (isRecord(message) && typeof message.content === "string" && message.content.length > 0) {
        return message.content;
      }
    }
    if (typeof output.content === "string" && output.content.length > 0) return output.content;
    if (typeof output.text === "string" && output.text.length > 0) return output.text;
    return null;
  }

  function summarizeObservedTelemetry(telemetry: unknown): string | null {
    if (!isRecord(telemetry)) return null;
    const rec = telemetry as Record<string, unknown>;
    const status = typeof rec.status === "string" ? rec.status : "unknown";
    const reason = typeof rec.unavailable_reason === "string" && rec.unavailable_reason
      ? ` reason=${rec.unavailable_reason}`
      : "";
    const call = rec.call_telemetry;
    if (!isRecord(call)) {
      return `status=${status}${reason}`;
    }
    const clauses = (call as Record<string, unknown>).clauses;
    if (!Array.isArray(clauses) || clauses.length === 0) {
      return `status=${status}${reason}`;
    }
    let latencyMs = 0;
    let hasLatency = false;
    let peakCpu: number | null = null;
    let peakMem: number | null = null;
    for (const clause of clauses) {
      if (!isRecord(clause)) continue;
      const row = clause as Record<string, unknown>;
      if (typeof row.latency_ms === "number" && Number.isFinite(row.latency_ms)) {
        latencyMs += row.latency_ms;
        hasLatency = true;
      }
      if (typeof row.peak_cpu_cores === "number" && Number.isFinite(row.peak_cpu_cores)) {
        peakCpu = peakCpu === null ? row.peak_cpu_cores : Math.max(peakCpu, row.peak_cpu_cores);
      }
      if (typeof row.peak_memory_mb === "number" && Number.isFinite(row.peak_memory_mb)) {
        peakMem = peakMem === null ? row.peak_memory_mb : Math.max(peakMem, row.peak_memory_mb);
      }
    }
    const parts = [`status=${status}`];
    if (hasLatency) parts.push(`latency=${formatMs(latencyMs)}`);
    if (peakCpu !== null) parts.push(`cpu_peak=${formatNumber(peakCpu, 2)}cores`);
    if (peakMem !== null) parts.push(`mem_peak=${formatNumber(peakMem, 1)}MB`);
    if (reason) parts.push(reason.trim());
    return parts.join("; ");
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
    if (typeof params === "string") return `args="${truncateStr(oneLine(params), 200)}"`;
    if (isRecord(params)) {
      const keys = Object.keys(params as Record<string, unknown>);
      if (keys.length === 0) return "{}";
      // For known tools, print key fields
      const p = params as Record<string, unknown>;
      const cmd = p.command ?? p.cmd;
      if (typeof cmd === "string") return `command="${truncateStr(oneLine(cmd), 200)}"`;
      const filePath = p.file_path ?? p.path ?? p.filePath ?? p.file;
      if (typeof filePath === "string") {
        const content = p.content ?? p.text;
        const contentLen = typeof content === "string" ? ` (${content.length} chars)` : "";
        return `path="${truncateStr(oneLine(filePath), 120)}"${contentLen}`;
      }
      // Generic: list key=value pairs
      const entries = keys.slice(0, 3).map(k => {
        const v = p[k];
        const vs = typeof v === "string"
          ? truncateStr(oneLine(v), 80)
          : (typeof v === "object" ? "{...}" : String(v));
        return `${k}=${vs}`;
      });
      return entries.join(", ") + (keys.length > 3 ? ` +${keys.length - 3} more` : "");
    }
    return `args="${truncateStr(oneLine(String(params)), 200)}"`;
  }

  function summarizeToolResult(event: unknown): string {
    if (!isRecord(event)) return "";
    const result = event.result ?? event.output ?? event.response;
    if (result === null || result === undefined) return "";
    if (typeof result === "string") return truncateStr(oneLine(result), 120);
    if (isRecord(result)) {
      const text = result.text ?? result.content ?? result.message ?? result.stdout;
      if (typeof text === "string") return truncateStr(oneLine(text), 120);
    }
    return "";
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
    const identity = hookIdentity(event, context, runtimeId, gatewayId);
    const correlationId = correlationKey(identity, toolCallId);

    // ── verbose console: tool call ──
    if (toolCallId) pendingToolNames.set(correlationId, toolName);
    const paramStr = summarizeToolParams(event);
    consoleVerbose(`■ ${toolName}${paramStr ? ` ${paramStr}` : ""}`);

    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const {runId, sessionId, agentId} = identity;
    const traceId = runId ?? sessionId ?? "unknown-run";
    const registryKey = correlationScopeKey(identity);

    // Resolve parent span
    let parentSpanId: string | null = null;
    let correlationStatus: "resolved" | "unresolved" = "unresolved";
    let correlationReason: string | null = null;
    if (toolCallId && registry) {
      parentSpanId = registry.getToolCallParent(correlationId);
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
        identityKey: registryKey,
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
        sequence_no: registry?.getSpan(registryKey, spanId)?.sequenceNo ?? 0,
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
      const w = await writerManager.get(writerScope(identity));
      if (w) {
        w.writeRecord(spanStart);
        if (registry) registry.markStartWritten(registryKey, spanId);
      }
    }

    // Original sidecar logic
    const payload = buildToolBefore(event, context, config, runtimeId, gatewayId, runtimeRepo);
    payload.resource_scope = buildTrustedResourceScope(event, context) ?? buildRuntimeResourceScope(toolName);
    let decisionOverheadNs = 0n;
    try {
      const tDecide = monotonicNowNs();
      const decision = await client.decide(payload);
      decisionOverheadNs += monotonicNowNs() - tDecide;
      consoleVerbose(summarizePrediction(decision));
      if (config.mode === "enforce" && decision.action === "block") {
        return {
          block: true,
          blockReason: decision.reason
        };
      }
      const tInst = monotonicNowNs();
      const instrumentation = await instrumentExecParams(event, context, payload, decision, client, config);
      decisionOverheadNs += monotonicNowNs() - tInst;
      if (toolCallId) {
        pendingToolOverhead.set(correlationId, {decisionNs: decisionOverheadNs});
      }
      correlation.set(
        correlationId,
        decision.decision_id,
        decision.lease_id,
        instrumentation.executionId,
        toolCallId,
      );

      // Store command variants for span_end
      if (registry) {
        const span = registry.getSpan(registryKey, spanId);
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
      logger.warn("ClawTune decision failed", classifyError(error));

      // ── failOpen with execution instrumentation ──────────────────────
      // When the sidecar's decision endpoint is unreachable or times out
      // the tool still runs (failOpen).  Execution registration and
      // launcher wrapping should also proceed so that PID / cgroup /
      // resource monitoring does not silently degrade to "unattributed"
      // on every decision failure.
      if (config.executionBackend !== "hook-only") {
        try {
          const tInst = monotonicNowNs();
          const instrumentation = await instrumentExecParams(
            event, context, payload, /* decision */ null, client, config,
          );
          decisionOverheadNs += monotonicNowNs() - tInst;
          if (toolCallId) {
            pendingToolOverhead.set(correlationId, {decisionNs: decisionOverheadNs});
          }
          if (instrumentation.executionId !== null) {
            correlation.set(
              correlationId,
              /* decisionId */ null,
              /* leaseId */ null,
              instrumentation.executionId,
              toolCallId,
            );
          }
          if (registry) {
            const span = registry.getSpan(registryKey, spanId);
            if (span) {
              span.metadata = {
                requestedCommand: instrumentation.requestedCommand,
                effectiveCommand: instrumentation.effectiveCommand,
                payloadCommand: instrumentation.payloadCommand,
                executionId: instrumentation.executionId,
              };
            }
          }
          if (instrumentation.params !== null) {
            const sandboxParams = normalizeSandboxToolParams(
              instrumentation.params,
              toolName,
            );
            return {params: sandboxParams.params ?? instrumentation.params};
          }
        } catch {
          // Execution registration itself failed — fall through to the
          // bare failOpen path below.
        }
      }

      const sandboxParams = normalizeSandboxToolParams(cloneEventParams(event), toolName);
      if ((config.mode === "observe" || config.failOpen) && sandboxParams.changed && sandboxParams.params !== null) {
        return {params: sandboxParams.params};
      }
      if (config.mode === "observe" || config.failOpen) return undefined;
      return {
        block: true,
        blockReason: "ClawTune Sidecar unavailable and failOpen=false."
      };
    }
  });

  // ── after_tool_call ───────────────────────────────────────────────

  api.on("after_tool_call", async (event: unknown, context: unknown) => {
    dumpHookShape(event, context, "after_tool_call");
    const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
    const toolCallId = extractString(event, ["tool_call_id", "toolCallId", "id"]);
    const identity = hookIdentity(event, context, runtimeId, gatewayId);
    const correlationId = correlationKey(identity, toolCallId);
    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const {runId} = identity;
    const traceId = runId ?? identity.sessionId ?? "unknown-run";
    const registryKey = correlationScopeKey(identity);
    const spanId = toolCallId ?? traceId;

    const endWall = wallClockNowNs();
    const endMono = monotonicNowNs();

    // Look up the active span for start times
    const activeSpan = registry?.endSpan(registryKey, spanId) ?? null;

    // Clean up parent mapping
    if (toolCallId && registry) {
      registry.clearToolCallParent(correlationId);
    }
    // Clean up pending tool name mapping
    if (toolCallId) pendingToolNames.delete(correlationId);
    const pendingOverhead = toolCallId ? pendingToolOverhead.get(correlationId) : undefined;
    if (toolCallId) pendingToolOverhead.delete(correlationId);
    const decisionOverheadNs = pendingOverhead?.decisionNs ?? 0n;

    const startMono = activeSpan?.startMonotonicTimeNs ?? endMono;
    const startWall = activeSpan?.startWallTimeNs ?? endWall;
    const parentSpanId = activeSpan?.parentSpanId ?? null;
    const durNs = durationNs(startMono, endMono);

    // Original sidecar logic
    const completion = buildCompletion(
      event,
      context,
      correlation.take(correlationId, toolCallId),
      config,
      runtimeId,
      gatewayId,
      runtimeRepo,
    );
    completion.resource_scope = buildTrustedResourceScope(event, context) ?? buildRuntimeResourceScope(toolName);
    if (completion.resource_scope === null && completion.execution_id !== null) {
      try {
        completion.resource_scope = await client.getExecutionScope(completion.execution_id);
      } catch (error) {
        logger.warn("ClawTune execution scope lookup failed", classifyError(error));
      }
    }
    // Precise tool-time split: separate the OpenClaw-reported tool action
    // from the plugin's own sidecar/plugin round-trip overhead.
    //   completion.duration_ms        = OpenClaw-reported tool action duration
    //   decisionOverheadNs            = before-hook sidecar RTT (decide + instrument)
    //   completionOverheadNs          = after-hook sidecar RTT (report + telemetry)
    // Fields set before reportCompletion reach the sidecar trace.  The
    // completion RTT is only known after the report is sent, so
    // sidecar_overhead_ns (what the sidecar records) is the before-hook
    // harness overhead; completion_duration_ns is recorded in the plugin's
    // own trace resources.
    const openclawActionNs =
      completion.duration_ms > 0
        ? BigInt(Math.trunc(completion.duration_ms)) * 1_000_000n
        : null;
    // Assign before reportCompletion so the sidecar trace records
    // the split (the payload is serialized at report time).
    completion.plugin_window_ns = durNs.toString();
    completion.tool_body_ns = openclawActionNs === null ? null : openclawActionNs.toString();
    completion.decision_duration_ns = decisionOverheadNs.toString();
    completion.sidecar_overhead_ns = decisionOverheadNs.toString();
    let completionOverheadNs = 0n;
    let toolResourceTelemetry: unknown | null = null;
    try {
      const tReport = monotonicNowNs();
      await client.reportCompletion(completion);
      completionOverheadNs += monotonicNowNs() - tReport;
    } catch (error) {
      logger.warn("ClawTune completion report failed", classifyError(error));
    }
    if (completion.execution_id !== null) {
      try {
        const tTel = monotonicNowNs();
        toolResourceTelemetry = await client.getExecutionTelemetry(completion.execution_id);
        completionOverheadNs += monotonicNowNs() - tTel;
      } catch (error) {
        logger.warn("ClawTune execution telemetry lookup failed", classifyError(error));
      }
    }
    completion.completion_duration_ns = completionOverheadNs.toString();

    // Determine status code
    const toolExitCode = extractToolExitCode(completion.raw_result, completion.tool_name);
    const toolSucceeded = completion.succeeded && (toolExitCode === null || toolExitCode === 0);

    // ── verbose console: concise tool result ──
    const durMs = completion.duration_ms ?? Math.round(Number(durNs) / 1e6);
    const statusStr = completion.succeeded ? "ok" : (completion.error_type ?? "failed");
    const resultStr = summarizeToolResult(event);
    consoleVerbose(
      `tool ${toolName}: ${statusStr} in ${durMs}ms${resultStr ? ` · result="${resultStr}"` : ""}`,
    );
    const observedSummary = summarizeObservedTelemetry(toolResourceTelemetry);
    if (observedSummary !== null) {
      consoleVerbose(`observed: ${observedSummary}`);
    }

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
      plugin_window_ns: durNs.toString(),
      tool_body_ns: completion.tool_body_ns ?? null,
      decision_duration_ns: completion.decision_duration_ns ?? null,
      completion_duration_ns: completion.completion_duration_ns ?? null,
      sidecar_overhead_ns: completion.sidecar_overhead_ns ?? null,
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
      const w = await writerManager.get(writerScope(identity, finalSessionId, finalAgentId));
      if (w) w.writeRecord(spanEnd);
    }
  });

  // ── model_call_started ────────────────────────────────────────────

  let llmSeqCounter = 0;

  api.on("model_call_started", async (event: unknown, context: unknown) => {
    dumpHookShape(event, context, "model_call_started");
    const callId = extractString(event, ["call_id", "callId", "id"]);
    const identity = hookIdentity(event, context, runtimeId, gatewayId);
    const callCorrelationId = correlationKey(
      identity,
      callId === null ? null : `__llm_call__${callId}`,
    );
    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const {runId, sessionId, agentId} = identity;
    const model = extractString(event, ["model"]) ?? "unknown-model";
    const provider = extractString(event, ["provider"]);
    const traceId = runId ?? sessionId ?? "unknown-run";
    const registryKey = correlationScopeKey(identity);

    // ── verbose console: turn start ──
    turnCounter++;
    consoleVerbose(`turn ${turnCounter}: model ${model}`);

    llmSeqCounter++;
    const spanId = callId ?? `${traceId}:model:${llmSeqCounter}`;

    const startWall = wallClockNowNs();
    const startMono = monotonicNowNs();

    if (registry) {
      registry.beginSpan({
        identityKey: registryKey,
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
        sequence_no: registry?.getSpan(registryKey, spanId)?.sequenceNo ?? 0,
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
      const w = await writerManager.get(writerScope(identity));
      if (w) {
        w.writeRecord(spanStart);
        if (registry) registry.markStartWritten(registryKey, spanId);
      }
    }

    // Original sidecar logic
    await reportModel(client, logger, event, context, "model_call_started", config, runtimeId, gatewayId, runtimeRepo);

    // Store call_id -> span_id mapping for model_call_ended
    if (callId && registry) {
      // Store in a side map (we can use tool_call_parent with a special prefix)
      registry.setToolCallParent(callCorrelationId, spanId, registryKey);
    }
  });

  // ── model_call_ended ──────────────────────────────────────────────

  api.on("model_call_ended", async (event: unknown, context: unknown) => {
    dumpHookShape(event, context, "model_call_ended");
    const callId = extractString(event, ["call_id", "callId", "id"]);
    const identity = hookIdentity(event, context, runtimeId, gatewayId);
    const callCorrelationId = correlationKey(
      identity,
      callId === null ? null : `__llm_call__${callId}`,
    );
    // Use OpenClaw-provided IDs only. No self-generated fallback.
    const {runId} = identity;
    const traceId = runId ?? identity.sessionId ?? "unknown-run";
    const registryKey = correlationScopeKey(identity);

    // Look up the span_id from the started event
    let spanId = callId ?? "";
    if (callId && registry) {
      const mappedSpanId = registry.getToolCallParent(callCorrelationId);
      if (mappedSpanId) {
        spanId = mappedSpanId;
        registry.clearToolCallParent(callCorrelationId);
      }
    }
    if (!spanId) {
      llmSeqCounter++;
      spanId = `${traceId}:model:${llmSeqCounter}`;
    }

    const endWall = wallClockNowNs();
    const endMono = monotonicNowNs();

    const activeSpan = registry?.endSpan(registryKey, spanId) ?? null;
    const startMono = activeSpan?.startMonotonicTimeNs ?? endMono;
    const durNs = durationNs(startMono, endMono);

    const model = extractString(event, ["model"]) ?? activeSpan?.name ?? "unknown-model";
    const outcome = extractString(event, ["outcome", "status"]);
    const durationMs = extractNumber(event, ["duration_ms", "durationMs"]);

    const modelOutput = extractModelOutput(event);
    const textContent = extractTextContent(modelOutput);
    if (textContent) {
      consoleVerbose(`LLM: "${truncateStr(oneLine(textContent), 30)}"`);
    }
    const displayToolCalls = extractToolCallsForDisplay(modelOutput);
    for (const tc of displayToolCalls) {
      consoleVerbose(`next tool: ${tc.name}`);
    }

    // Extract tool calls from the response to set up parent mapping
    if (registry) {
      const toolCalls = extractToolCallsFromResponse(event);
      for (const tcId of toolCalls) {
        registry.setToolCallParent(correlationKey(identity, tcId), spanId, registryKey);
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
      const w = await writerManager.get(writerScope(identity, finalSessionId, finalAgentId));
      if (w) w.writeRecord(spanEnd);
    }

    // Original sidecar logic
    await reportModel(client, logger, event, context, "model_call_ended", config, runtimeId, gatewayId, runtimeRepo);
  });

  async function finalizeRunTrace(identity: HookIdentity): Promise<void> {
    if (identity.runId === null) return;
    // Model and tool hooks can describe the same run with different agent-id
    // detail. Treat Gateway/runtime/session/run as the lifecycle boundary so
    // neither variant survives agent_end.
    const activeSpans = registry.clearRunsWhere((identityKey) => (
      correlationScopeMatchesRun(identityKey, identity)
    ));

    try {
      if (traceCfg.trace_dir && activeSpans.length > 0) {
        const endWall = wallClockNowNs();
        const endMono = monotonicNowNs();
        for (const span of activeSpans) {
          // Append the terminal record to the same agent/session writer variant
          // that received span_start, then close every variant below.
          const writer = await writerManager.get(writerScope(
            identity,
            span.sessionId,
            span.agentId,
          ));
          if (writer) {
            writer.writeRecord(interruptedSpanEnd(
              span,
              endWall,
              endMono,
              "agent run ended before span completion",
            ));
          }
        }
      }
    } finally {
      // clearRunsWhere already released spans, completed-span deduplication,
      // sequence counters, and parent mappings for every identity variant.
      // Always release handles even if terminal-record creation fails.
      if (traceCfg.trace_dir) await writerManager.closeRun(writerScope(identity));
    }
  }

  async function finalizeSessionTrace(identity: HookIdentity): Promise<void> {
    if (identity.sessionId === null) return;
    const activeSpans = registry.clearRunsWhere((identityKey) => (
      correlationScopeMatchesSession(identityKey, identity)
    ));

    try {
      if (traceCfg.trace_dir && activeSpans.length > 0) {
        const endWall = wallClockNowNs();
        const endMono = monotonicNowNs();
        for (const span of activeSpans) {
          // A session-level fallback is primarily for hooks without runId, so
          // retain the lifecycle hook's writer key while selecting the span's
          // agent-id variant.
          const writer = await writerManager.get(writerScope(
            identity,
            span.sessionId,
            span.agentId,
          ));
          if (writer) {
            writer.writeRecord(interruptedSpanEnd(
              span,
              endWall,
              endMono,
              "session ended before span completion",
            ));
          }
        }
      }
    } finally {
      if (traceCfg.trace_dir) await writerManager.closeSession(writerScope(identity));
    }
  }

  // A long-lived Gateway can execute thousands of runs. Finalize each run as
  // soon as OpenClaw reports agent_end instead of retaining one writer and
  // registry generation until the Gateway process eventually exits.
  api.on("agent_end", async (event: unknown, context: unknown) => {
    await finalizeRunTrace(hookIdentity(event, context, runtimeId, gatewayId));
  });

  // Older/partial hook payloads may not expose runId. Session end is the safe
  // fallback for writer and registry state, and is idempotent after agent_end.
  api.on("session_end", async (event: unknown, context: unknown) => {
    await finalizeSessionTrace(hookIdentity(event, context, runtimeId, gatewayId));
  });

  // ── Shutdown handling ────────────────────────────────────────────────
  // Write interrupted spans and stop auto-started sidecar when plugin is
  // being unloaded.
  let shuttingDown = false;

  async function performShutdown(): Promise<void> {
    if (shuttingDown) return;
    shuttingDown = true;

    // Wait for the sidecar launch to complete (or fail) so the
    // launcher reference is populated before we try to clean it up.
    if (sidecarLaunchPromise) {
      try {
        await sidecarLaunchPromise;
      } catch {
        // Already logged in the launch promise catch handler
      }
    }

    // Clean up auto-started sidecar
    if (sidecarLauncher) {
      sidecarLauncher.cleanup();
    }

    try {
      if (traceCfg.trace_dir && registry.listActiveSpans().length > 0) {
        const activeSpans = registry.listActiveSpans();
        const endWall = wallClockNowNs();
        const endMono = monotonicNowNs();

        for (const span of activeSpans) {
          const identity: HookIdentity = {
            gatewayId,
            runtimeId: runtimeIdForSession(runtimeId, gatewayId, span.sessionId),
            agentId: span.agentId,
            sessionId: span.sessionId,
            sessionKey: span.sessionId,
            runId: span.runId,
          };
          const writer = await writerManager.get(writerScope(identity));
          if (writer) {
            writer.writeRecord(interruptedSpanEnd(
              span,
              endWall,
              endMono,
              "plugin shutdown before span completion",
            ));
          }
        }
      }
    } finally {
      registry.clear();
      await writerManager.closeAll();
    }
  }

  process.on("beforeExit", performShutdown);

  // On SIGINT (Ctrl+C) / SIGTERM, Node does not exit automatically when a
  // listener is registered.  Run the same cleanup and then exit.
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.once(signal, () => {
      performShutdown().finally(() => {
        // Preserve conventional signal exit status instead of masking a
        // terminated OpenClaw process as a successful exit.
        process.exit(signal === "SIGINT" ? 130 : 143);
      });
    });
  }
}
});

// ── Helper functions ───────────────────────────────────────────────────

type HookIdentity = {
  gatewayId: string;
  runtimeId: string;
  agentId: string | null;
  sessionId: string | null;
  sessionKey: string | null;
  runId: string | null;
};

function runtimeIdForSession(
  fallbackRuntimeId: string,
  gatewayId: string,
  sessionId: string | null,
): string {
  if (sessionId === null) return fallbackRuntimeId;
  return `session-${stableDigest([gatewayId, sessionId]).replace(/^sha256:/, "").slice(0, 48)}`;
}

function interruptedSpanEnd(
  span: ActiveSpan,
  endWall: bigint,
  endMono: bigint,
  message: string,
): SpanEndRecord {
  const durNs = durationNs(span.startMonotonicTimeNs, endMono);
  return {
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
    status: {code: "interrupted", message},
    output: {},
    execution: {mode: null, execution_id: null},
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
}

/** Resolve one hook's Gateway -> runtime -> session -> run owner chain. */
function hookIdentity(
  event: unknown,
  context: unknown,
  fallbackRuntimeId: string,
  gatewayId: string,
): HookIdentity {
  const runtimeSessionKey = getRuntimeSessionKey();
  const sessionKey = extractString(event, ["session_key", "sessionKey"])
    ?? extractString(context, ["sessionKey", "session_key"])
    ?? runtimeSessionKey;
  const sessionId = extractString(event, ["session_id", "sessionId"])
    ?? extractString(context, ["sessionId", "session_id"])
    ?? sessionKey;
  const explicitRuntimeId = extractString(event, ["runtime_id", "runtimeId"])
    ?? extractString(context, ["runtimeId", "runtime_id"]);
  const configuredRuntimeId = process.env.CLAWTUNE_RUNTIME_ID?.trim() || null;
  const derivedRuntimeId = runtimeIdForSession(fallbackRuntimeId, gatewayId, sessionId);
  return {
    gatewayId: gatewayId.slice(0, 128),
    runtimeId: (explicitRuntimeId ?? configuredRuntimeId ?? derivedRuntimeId).slice(0, 128),
    agentId: extractString(event, ["agent_id", "agentId"])
      ?? extractString(context, ["agentId", "agent_id"])
      ?? getRuntimeAgentId(),
    sessionId,
    sessionKey,
    runId: extractString(event, ["run_id", "runId"])
      ?? extractString(context, ["runId", "run_id"])
      ?? getRuntimeRunId(),
  };
}

function correlationScopeKey(identity: HookIdentity): string {
  return JSON.stringify([
    identity.gatewayId,
    identity.runtimeId,
    identity.agentId,
    identity.sessionId,
    identity.runId,
  ]);
}

function correlationScopeMatchesRun(
  identityKey: string,
  identity: HookIdentity,
): boolean {
  try {
    const value: unknown = JSON.parse(identityKey);
    if (!Array.isArray(value) || value.length !== 5) return false;
    return value[0] === identity.gatewayId
      && value[1] === identity.runtimeId
      && value[3] === identity.sessionId
      && value[4] === identity.runId;
  } catch {
    return false;
  }
}

function correlationScopeMatchesSession(
  identityKey: string,
  identity: HookIdentity,
): boolean {
  try {
    const value: unknown = JSON.parse(identityKey);
    if (!Array.isArray(value) || value.length !== 5) return false;
    return value[0] === identity.gatewayId
      && value[1] === identity.runtimeId
      && value[3] === identity.sessionId;
  } catch {
    return false;
  }
}

function correlationKey(identity: HookIdentity, callId: string | null): string {
  return JSON.stringify([
    identity.gatewayId,
    identity.runtimeId,
    identity.agentId,
    identity.sessionId,
    identity.runId,
    callId,
  ]);
}

function common(
  event: unknown,
  context: unknown,
  runtimeId: string,
  gatewayId: string,
  repo: string | null,
): CommonEvent {
  const identity = hookIdentity(event, context, runtimeId, gatewayId);
  return {
    schema_version: "clawtune.v1",
    event_id: randomUUID(),
    occurred_at: new Date().toISOString(),
    plugin_version: pluginVersion,
    run_id: identity.runId,
    session_id: identity.sessionId,
    session_key: identity.sessionKey,
    agent_id: identity.agentId,
    gateway_id: identity.gatewayId,
    runtime_id: identity.runtimeId,
    repo,
  };
}

function buildToolBefore(
  event: unknown,
  context: unknown,
  config: PluginConfig,
  runtimeId: string,
  gatewayId: string,
  repo: string | null,
): ToolBeforeRequest {
  const params = isRecord(event) ? (event as Record<string, unknown>).params ?? (event as Record<string, unknown>).arguments ?? (event as Record<string, unknown>).input ?? null : null;
  const safeParams = redact(params);
  const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
  const includeRaw = config.trace?.include_raw_events === true;
  const rawEvent = includeRaw && isRecord(event)
    ? (config.trace.redact_sensitive_data ? redact(event) : jsonSafe(event))
    : null;
  return {
    ...common(event, context, runtimeId, gatewayId, repo),
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

function buildCompletion(
  event: unknown,
  context: unknown,
  prior: {decisionId: string | null; leaseId: string | null; executionId: string | null} | null,
  config: PluginConfig,
  runtimeId: string,
  gatewayId: string,
  repo: string | null,
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
    ...common(event, context, runtimeId, gatewayId, repo),
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
  context: unknown,
  eventType: "model_call_started" | "model_call_ended",
  config: PluginConfig,
  runtimeId: string,
  gatewayId: string,
  repo: string | null,
): Promise<void> {
  try {
    const includeRaw = config.trace?.include_raw_events === true;
    const rawEvent = includeRaw && isRecord(event)
      ? (config.trace.redact_sensitive_data ? redact(event) : jsonSafe(event))
      : null;
    const payload: ModelEvent = {
      ...common(event, context, runtimeId, gatewayId, repo),
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
    logger.warn("ClawTune model report failed", classifyError(error));
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
    "CLAWTUNE_RUN_ID",
  ]) ?? argvValue("--run-id");
}

function getRuntimeAgentId(): string | null {
  return firstEnvString([
    "OPENCLAW_AGENT_ID",
    "CLAWTUNE_AGENT_ID",
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
