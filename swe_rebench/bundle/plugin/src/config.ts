import type {PluginConfig} from "./contracts.js";

const defaults: PluginConfig = {
  endpoint: "http://localhost:8765",
  mode: "observe",
  decisionTimeoutMs: 800,
  reportTimeoutMs: 800,
  failOpen: true,
  sendRawParams: false,
  recordRawTrace: false,
  logLevel: "info",
  consoleMode: "verbose",
  executionBackend: "managed-wrapper",
  launcherPath: "/opt/claw/bin/claw-launch",
  launcherInterpreter: null,
  collectorSocket: "/run/claw/collector.sock",
  instrumentHosts: ["gateway"],
  instrumentTools: ["exec"],
  enableCgroup: true,
  enableAffinity: true,
  enableNuma: true,
  profilingMode: "off",
  securityBoundaryAccepted: true,
  // Automatic startup is opt-in at the package level. ClawTune setup enables
  // it after constructing and validating the privileged Python/BCC runtime.
  autoStartSidecar: false,
  sidecarStartupTimeoutMs: 60_000,
  sidecarCommand: "",
  repo: null,
  trace: {
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
  },
};

export function loadConfig(input: unknown): PluginConfig {
  const raw = isRecord(input) ? input : {};
  const rawTrace = isRecord(raw.trace) ? raw.trace : {};
  const legacyTrace = legacyTraceOverrides(raw);
  const env = envOverrides();
  const envTrace = isRecord(env.trace) ? env.trace : {};
  const config = {
    ...defaults,
    ...raw,
    ...env,
    trace: {
      ...defaults.trace,
      ...rawTrace,
      ...legacyTrace,
      ...envTrace,
    },
  };
  if (config.mode !== "observe" && config.mode !== "enforce") {
    throw new Error(`invalid mode: ${String(config.mode)}`);
  }
  if (!Number.isInteger(config.decisionTimeoutMs) || config.decisionTimeoutMs <= 0) {
    throw new Error("decisionTimeoutMs must be a positive integer");
  }
  if (!Number.isInteger(config.reportTimeoutMs) || config.reportTimeoutMs <= 0) {
    throw new Error("reportTimeoutMs must be a positive integer");
  }
  if (typeof config.failOpen !== "boolean") {
    throw new Error("failOpen must be a boolean");
  }
  if (typeof config.sendRawParams !== "boolean") {
    throw new Error("sendRawParams must be a boolean");
  }
  if (typeof config.recordRawTrace !== "boolean") {
    throw new Error("recordRawTrace must be a boolean");
  }
  if (!["hook-only", "marker", "managed-wrapper"].includes(String(config.executionBackend))) {
    throw new Error(`invalid executionBackend: ${String(config.executionBackend)}`);
  }
  if (!["off", "proc", "perf", "ksys", "vtune"].includes(String(config.profilingMode))) {
    throw new Error(`invalid profilingMode: ${String(config.profilingMode)}`);
  }
  if (typeof config.launcherPath !== "string" || config.launcherPath.length === 0) {
    throw new Error("launcherPath must be a non-empty string");
  }
  if (config.launcherInterpreter !== null
      && (typeof config.launcherInterpreter !== "string" || config.launcherInterpreter.length === 0)) {
    throw new Error("launcherInterpreter must be null or a non-empty string");
  }
  if (typeof config.collectorSocket !== "string" || config.collectorSocket.length === 0) {
    throw new Error("collectorSocket must be a non-empty string");
  }
  if (!Array.isArray(config.instrumentHosts) || !config.instrumentHosts.every((item) => typeof item === "string")) {
    throw new Error("instrumentHosts must be an array of strings");
  }
  if (!Array.isArray(config.instrumentTools) || !config.instrumentTools.every((item) => typeof item === "string")) {
    throw new Error("instrumentTools must be an array of strings");
  }
  if (typeof config.enableCgroup !== "boolean") {
    throw new Error("enableCgroup must be a boolean");
  }
  if (typeof config.enableAffinity !== "boolean") {
    throw new Error("enableAffinity must be a boolean");
  }
  if (typeof config.enableNuma !== "boolean") {
    throw new Error("enableNuma must be a boolean");
  }
  if (typeof config.autoStartSidecar !== "boolean") {
    throw new Error("autoStartSidecar must be a boolean");
  }
  if (!Number.isInteger(config.sidecarStartupTimeoutMs)
      || config.sidecarStartupTimeoutMs < 1_000
      || config.sidecarStartupTimeoutMs > 600_000) {
    throw new Error(
      "sidecarStartupTimeoutMs must be an integer between 1000 and 600000"
    );
  }
  if (typeof config.sidecarCommand !== "string") {
    throw new Error("sidecarCommand must be a string");
  }
  if (config.executionBackend === "managed-wrapper" && config.securityBoundaryAccepted !== true) {
    throw new Error("managed-wrapper requires securityBoundaryAccepted=true");
  }
  if (config.executionBackend === "managed-wrapper") {
    validateManagedWrapperLauncherPath(config.launcherPath);
    if (config.launcherInterpreter !== null) {
      validateAbsolutePath(config.launcherInterpreter, "launcherInterpreter");
    }
  }
  return config as PluginConfig;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function envOverrides(): Partial<PluginConfig> {
  const output: Partial<PluginConfig> = {};
  setString(output, "endpoint", schedulerEnv("ENDPOINT"));
  setString(output, "mode", schedulerEnv("MODE"));
  setString(output, "consoleMode", schedulerEnv("CONSOLE_MODE"));
  setString(output, "launcherPath", schedulerEnv("LAUNCHER_PATH"));
  setString(output, "executionBackend", schedulerEnv("EXECUTION_BACKEND"));
  setBoolean(output, "failOpen", schedulerEnv("FAIL_OPEN"));
  setBoolean(output, "sendRawParams", schedulerEnv("SEND_RAW_PARAMS"));
  setBoolean(output, "recordRawTrace", schedulerEnv("RECORD_RAW_TRACE"));
  setBoolean(
    output,
    "securityBoundaryAccepted",
    schedulerEnv("SECURITY_BOUNDARY_ACCEPTED")
  );
  setBoolean(output, "autoStartSidecar", schedulerEnv("AUTO_START_SIDECAR"));
  setString(output, "sidecarCommand", schedulerEnv("SIDECAR_COMMAND"));
  setString(output, "repo", schedulerEnv("REPO"));
  const trace: Record<string, unknown> = {};
  const traceDir = schedulerEnv("PLUGIN_TRACE_DIR");
  if (traceDir !== undefined && traceDir.length > 0) trace.trace_dir = traceDir;
  const recordRaw = parseBoolean(schedulerEnv("RECORD_RAW_TRACE"));
  if (recordRaw !== null) {
    trace.include_raw_events = recordRaw;
    trace.include_llm_messages = recordRaw;
    trace.include_tool_outputs = recordRaw;
  }
  if (Object.keys(trace).length > 0) {
    (output as Record<string, unknown>).trace = trace;
  }
  return output;
}

function schedulerEnv(suffix: string): string | undefined {
  return process.env[`OPENCLAW_AGENT_SCHEDULER_${suffix}`];
}

function legacyTraceOverrides(raw: Record<string, unknown>): Record<string, unknown> {
  const output: Record<string, unknown> = {};
  const sendRawParams = booleanValue(raw.sendRawParams);
  const recordRawTrace = booleanValue(raw.recordRawTrace);
  if (recordRawTrace === true) {
    output.include_raw_events = true;
    output.include_llm_messages = true;
    output.include_tool_outputs = true;
  }
  if (sendRawParams === true) {
    output.include_tool_outputs = true;
  }
  return output;
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function setString<K extends keyof PluginConfig>(
  output: Partial<PluginConfig>,
  key: K,
  value: string | undefined
): void {
  if (value !== undefined && value.length > 0) {
    (output as Record<string, unknown>)[key] = value;
  }
}

function setBoolean<K extends keyof PluginConfig>(
  output: Partial<PluginConfig>,
  key: K,
  value: string | undefined
): void {
  const parsed = parseBoolean(value);
  if (parsed === null) return;
  (output as Record<string, unknown>)[key] = parsed;
}

function parseBoolean(value: string | undefined): boolean | null {
  if (value === undefined || value.length === 0) return null;
  const normalized = value.toLowerCase();
  return ["1", "true", "yes", "on"].includes(normalized);
}

function validateManagedWrapperLauncherPath(value: string): void {
  if (value === "/absolute/path/to/claw-launch" || value.includes("<")) {
    throw new Error(
      "managed-wrapper launcherPath is still a placeholder; set it to `command -v claw-launch`"
    );
  }
  if (!value.startsWith("/")) {
    throw new Error("managed-wrapper launcherPath must be an absolute path");
  }
}

function validateAbsolutePath(value: string, key: string): void {
  if (!value.startsWith("/")) {
    throw new Error(`${key} must be an absolute path`);
  }
}
