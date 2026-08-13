import type {PluginConfig} from "./contracts.js";

const defaults: PluginConfig = {
  endpoint: "http://localhost:8765",
  mode: "observe",
  decisionTimeoutMs: 800,
  reportTimeoutMs: 800,
  failOpen: true,
  logLevel: "info",
  consoleMode: "verbose",
  executionBackend: "managed-wrapper",
  // Empty string = auto-resolve via `which clawtune-launch` at runtime.
  launcherPath: "",
  launcherInterpreter: null,
  collectorSocket: "/run/clawtune/collector.sock",
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
  const env = envOverrides();
  const envTrace = isRecord(env.trace) ? env.trace : {};
  const config = {
    ...defaults,
    ...raw,
    ...env,
    trace: {
      ...defaults.trace,
      ...rawTrace,
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
  if (!["hook-only", "marker", "managed-wrapper"].includes(String(config.executionBackend))) {
    throw new Error(`invalid executionBackend: ${String(config.executionBackend)}`);
  }
  if (!["off", "proc", "perf", "ksys", "vtune"].includes(String(config.profilingMode))) {
    throw new Error(`invalid profilingMode: ${String(config.profilingMode)}`);
  }
  if (typeof config.launcherPath !== "string") {
    throw new Error("launcherPath must be a string");
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
  setString(output, "endpoint", clawtuneEnv("ENDPOINT"));
  setString(output, "mode", clawtuneEnv("MODE"));
  setString(output, "consoleMode", clawtuneEnv("CONSOLE_MODE"));
  setString(output, "launcherPath", clawtuneEnv("LAUNCHER_PATH"));
  setString(output, "executionBackend", clawtuneEnv("EXECUTION_BACKEND"));
  setBoolean(output, "failOpen", clawtuneEnv("FAIL_OPEN"));
  setBoolean(
    output,
    "securityBoundaryAccepted",
    clawtuneEnv("SECURITY_BOUNDARY_ACCEPTED")
  );
  setBoolean(output, "autoStartSidecar", clawtuneEnv("AUTO_START_SIDECAR"));
  setString(output, "sidecarCommand", clawtuneEnv("SIDECAR_COMMAND"));
  setString(output, "repo", clawtuneEnv("REPO"));
  const trace: Record<string, unknown> = {};
  const traceDir = clawtuneEnv("PLUGIN_TRACE_DIR");
  if (traceDir !== undefined && traceDir.length > 0) trace.trace_dir = traceDir;
  if (Object.keys(trace).length > 0) {
    (output as Record<string, unknown>).trace = trace;
  }
  return output;
}

function clawtuneEnv(suffix: string): string | undefined {
  return process.env[`CLAWTUNE_${suffix}`];
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
  // Empty string is valid: the plugin resolves clawtune-launch from PATH at runtime.
  if (value.length === 0) return;
  if (value === "/absolute/path/to/clawtune-launch" || value.includes("<")) {
    throw new Error(
      "managed-wrapper launcherPath is still a placeholder; set it to an absolute path or leave empty for auto-resolve"
    );
  }
  if (!value.startsWith("/")) {
    throw new Error("managed-wrapper launcherPath must be an absolute path or empty for auto-resolve");
  }
}

function validateAbsolutePath(value: string, key: string): void {
  if (!value.startsWith("/")) {
    throw new Error(`${key} must be an absolute path`);
  }
}
