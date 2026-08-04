import {randomUUID} from "node:crypto";
import {execFileSync} from "node:child_process";
import type {PluginConfig, ResourceScope, ToolBeforeRequest, ToolDecision} from "./contracts.js";
import {isRecord} from "./config.js";
import {stableDigest} from "./redaction.js";

export type ExecutionRegistrar = {
  registerExecution(payload: Parameters<ToolRegistrationFunction>[0]): Promise<{one_time_token: string}>;
};

type ToolRegistrationFunction = (payload: {
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
}) => Promise<{one_time_token: string}>;

export type InstrumentResult = {
  params: Record<string, unknown> | null;
  executionId: string | null;
  /** Original command the agent requested. */
  requestedCommand: string | null;
  /** Command that OpenClaw will actually run. */
  effectiveCommand: string | null;
  /** Command the launcher will execute as payload. */
  payloadCommand: string | null;
};

export async function instrumentExecParams(
  event: unknown,
  context: unknown,
  payload: ToolBeforeRequest,
  decision: ToolDecision | null,
  client: ExecutionRegistrar,
  config: PluginConfig
): Promise<InstrumentResult> {
  const empty: InstrumentResult = {
    params: null,
    executionId: null,
    requestedCommand: null,
    effectiveCommand: null,
    payloadCommand: null,
  };

  if (config.executionBackend === "hook-only") return empty;
  const shouldInstrumentResult = shouldInstrument(event, config);
  if (!shouldInstrumentResult) {
    if (config.logLevel === "debug") {
      const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
      const params = isRecord(event) ? event.params ?? event.arguments ?? event.input ?? null : null;
      const host = isRecord(params) && typeof params.host === "string" ? params.host : "gateway";
      console.error(
        `[clawtune] instrumentExecParams: shouldInstrument=false tool=${toolName} host=${host} ` +
        `instrumentTools=${JSON.stringify(config.instrumentTools)} instrumentHosts=${JSON.stringify(config.instrumentHosts)}`
      );
    }
    return empty;
  }
  const params = cloneRecord(isRecord(event) ? event.params ?? event.arguments ?? event.input ?? null : null);
  if (params === null || typeof params.command !== "string" || params.command.length === 0) {
    return empty;
  }
  const requestedCommand = params.command;
  const commandDigest = stableDigest(requestedCommand);
  const executionId = `exec-${randomUUID()}`;
  const runId = payload.run_id ?? extractString(context, ["runId", "run_id"]);
  const sessionKeyHash = payload.session_key === null ? null : stableDigest(payload.session_key);
  const workdirOverride = launcherWorkdirOverride();
  if (workdirOverride !== null) {
    params.workdir = workdirOverride;
    params.cwd = workdirOverride;
    if (params.host === "gateway") {
      delete params.host;
    }
    if (params.elevated === true) {
      delete params.elevated;
    }
  }
  let token: string | null = null;

  try {
    const registration = await client.registerExecution({
      execution_id: executionId,
      gateway_id: payload.gateway_id,
      runtime_id: payload.runtime_id,
      repo: payload.repo,
      agent_id: payload.agent_id,
      session_id: payload.session_id,
      tool_call_id: payload.tool_call_id,
      lease_id: decision?.lease_id ?? null,
      run_id: runId,
      session_key_hash: sessionKeyHash,
      command_digest: commandDigest,
      command: requestedCommand,
      workdir: typeof params.workdir === "string" ? params.workdir : null,
      host: typeof params.host === "string" ? params.host : "gateway",
      // placement_advice is deliberately observational in this MVP.  Only a
      // separately authorized placement object may reach the launcher.
      placement: decision?.placement ?? null,
      profiling: decision?.profiling ?? {
        mode: config.profilingMode,
        enable_cgroup: config.enableCgroup,
        enable_affinity: config.enableAffinity,
        enable_numa: config.enableNuma
      },
      backend: config.executionBackend
    });
    token = registration.one_time_token;
  } catch (error) {
    if (config.logLevel === "debug") {
      console.error(
        `[clawtune] instrumentExecParams: registerExecution failed executionId=${executionId} ` +
        `error=${error instanceof Error ? error.message : String(error)} ` +
        `failOpen=${config.failOpen} mode=${config.mode}`
      );
    }
    if (config.mode !== "observe" && !config.failOpen) throw error;
  }

  const inheritedLauncherEnv = launcherEnv();
  const launcherEndpoint = inheritedLauncherEnv.CLAW_SCHEDULER_ENDPOINT
    ?? inheritedLauncherEnv.OPENCLAW_SCHEDULER_ENDPOINT
    ?? config.endpoint;
  params.env = {
    ...safeExecEnv(params.env),
    ...inheritedLauncherEnv,
    CLAW_EXECUTION_ID: executionId,
    ...(token !== null && config.executionBackend === "managed-wrapper"
      ? {CLAW_EXECUTION_TOKEN: token}
      : {}),
    CLAW_SCHEDULER_ENDPOINT: launcherEndpoint,
    CLAW_GATEWAY_ID: payload.gateway_id ?? "",
    CLAW_RUNTIME_ID: payload.runtime_id ?? "",
    CLAW_AGENT_ID: payload.agent_id ?? "",
    CLAW_SESSION_ID: payload.session_id ?? "",
    CLAW_TOOL_CALL_ID: payload.tool_call_id ?? "",
    CLAW_RUN_ID: runId ?? "",
    CLAW_SESSION_HASH: sessionKeyHash ?? "",
    CLAW_COMMAND_DIGEST: commandDigest
  };

  let effectiveCommand: string | null = params.command as string;
  let payloadCommand: string | null = params.command as string;

  if (config.executionBackend === "managed-wrapper") {
    if (token === null) {
      // When execution registration fails but failOpen is active, still wrap
      // the command with the launcher so that cgroup isolation and resource
      // monitoring remain active.  The launcher receives the payload command
      // via CLAW_PAYLOAD_COMMAND and operates in degraded mode without
      // sidecar claim/started/exited reporting.
      if (config.mode === "observe" || config.failOpen) {
        if (config.logLevel === "debug") {
          console.error(
            `[clawtune] instrumentExecParams: degraded mode — wrapping with launcher ` +
            `despite null token (registration failed). executionId=${executionId}`
          );
        }
        const env = params.env as Record<string, unknown>;
        env.CLAW_PAYLOAD_COMMAND = requestedCommand;
        env.CLAW_DEGRADED = "1";
        effectiveCommand = buildLauncherCommand(config, executionId);
        params.command = effectiveCommand;
      } else {
        throw new Error("execution_registration_failed");
      }
    } else {
      effectiveCommand = buildLauncherCommand(config, executionId);
      params.command = effectiveCommand;
    }
    // payloadCommand stays as the original requestedCommand
  } else if (config.executionBackend === "marker") {
    // For marker backend, effective == payload == requested (command unchanged)
    effectiveCommand = requestedCommand;
    payloadCommand = requestedCommand;
  }

  return {
    params,
    executionId,
    requestedCommand,
    effectiveCommand,
    payloadCommand,
  };
}

export function buildTrustedResourceScope(event: unknown, context: unknown): ResourceScope | null {
  const scope = directRecord(event, ["execution_scope", "executionScope", "resource_scope", "resourceScope"])
    ?? directRecord(context, ["execution_scope", "executionScope", "resource_scope", "resourceScope"]);
  if (scope === null) return null;
  const rootPid = extractNumber(scope, ["root_pid", "rootPid"]);
  const pid = extractNumber(scope, ["pid", "process_id", "processId"]) ?? rootPid;
  const processStartTime = extractFiniteNumber(scope, ["process_start_time", "processStartTime"]);
  const rootStarttimeTicks = extractFiniteNumber(scope, ["root_starttime_ticks", "rootStarttimeTicks"]);
  const containerId = extractString(scope, ["container_id", "containerId"]);
  const cgroupPath = extractString(scope, ["cgroup_path", "cgroupPath"]);
  if (pid === null && processStartTime === null && containerId === null && cgroupPath === null) return null;
  return {
    pid,
    process_start_time: processStartTime,
    container_id: containerId,
    include_children: true,
    source: extractString(scope, ["source"]) ?? extractString(scope, ["attribution_source", "attributionSource"]),
    kind: extractString(scope, ["kind"]) === "cgroup-v2" ? "cgroup-v2" : "pid",
    execution_id: extractString(scope, ["execution_id", "executionId"]),
    root_pid: rootPid,
    root_starttime_ticks: rootStarttimeTicks,
    cgroup_path: cgroupPath,
    pid_namespace_inode: extractNumber(scope, ["pid_namespace_inode", "pidNamespaceInode"]),
    attribution_source: extractString(scope, ["attribution_source", "attributionSource"])
  };
}

function shouldInstrument(event: unknown, config: PluginConfig): boolean {
  const toolName = extractString(event, ["tool_name", "toolName", "name"]) ?? "unknown";
  if (!matchesList(config.instrumentTools, toolName)) return false;
  const params = isRecord(event) ? event.params ?? event.arguments ?? event.input ?? null : null;
  const host = isRecord(params) && typeof params.host === "string" ? params.host : "gateway";
  return matchesList(config.instrumentHosts, host);
}

function matchesList(values: string[], candidate: string): boolean {
  return values.includes("*") || values.includes(candidate);
}

function cloneRecord(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
}

function safeExecEnv(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {};
  const output: Record<string, unknown> = {};
  const blocked = new Set(["BASH_ENV", "ENV"]);
  for (const [key, item] of Object.entries(value)) {
    if (blocked.has(key)) continue;
    output[key] = item;
  }
  return output;
}

function launcherEnv(): Record<string, string> {
  const output: Record<string, string> = {};
  for (const key of [
    "CLAW_EXEC_WORKDIR",
    "CLAW_CGROUP_ROOT",
    "CLAW_CGROUP_PATH",
    "CLAW_CGROUP_REQUIRED",
    "CLAW_CGROUP_DEBUG",
    "CLAW_ENABLE_CGROUP",
    "CLAW_LAUNCH_MODE",
    "CLAW_LAUNCH_DEBUG",
    "CLAW_TASK_PYTHON",
    "CLAW_SCHEDULER_ENDPOINT",
    "OPENCLAW_SCHEDULER_ENDPOINT",
    "CLAW_SANDBOX_CONTAINER_ID",
    "AGENT_SCHEDULER_SANDBOX_CONTAINER_ID",
  ]) {
    const value = process.env[key];
    if (typeof value === "string" && value.length > 0) output[key] = value;
  }
  return output;
}

function launcherWorkdirOverride(): string | null {
  const value = process.env.CLAW_EXEC_WORKDIR;
  return typeof value === "string" && value.length > 0 ? value : null;
}

function buildLauncherCommand(config: PluginConfig, executionId: string): string {
  const resolvedPath = resolveLauncherPath(config.launcherPath);
  const launcherInvocation = [
    ...(config.launcherInterpreter === null ? [] : [shellQuote(config.launcherInterpreter)]),
    shellQuote(resolvedPath),
    "run",
    `--execution-id=${shellQuote(executionId)}`
  ].join(" ");
  if (config.launcherInterpreter === null) return launcherInvocation;
  // OpenClaw's exec transport treats direct shell-script invocations specially.
  // Use an inline shell payload so the launcher script is read by the trusted
  // interpreter without being reinterpreted as a system.run script target.
  return [
    shellQuote(config.launcherInterpreter),
    "-c",
    shellQuote(`exec ${launcherInvocation}`)
  ].join(" ");
}

/** Resolve claw-launch to an absolute path.  When the configured path is empty
 *  the plugin searches PATH; otherwise it returns the configured value as-is. */
function resolveLauncherPath(configured: string): string {
  if (configured.length > 0) return configured;
  try {
    const found = execFileSync("which", ["claw-launch"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 3000,
    }).trim();
    if (found) return found;
  } catch {
    // which failed — fall through to default
  }
  // Last resort: return the bare name; the shell may still resolve it.
  return "claw-launch";
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function directRecord(value: unknown, keys: string[]): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  for (const key of keys) {
    const item = value[key];
    if (isRecord(item)) return item;
  }
  return null;
}

function extractString(value: unknown, keys: string[]): string | null {
  if (!isRecord(value)) return null;
  for (const key of keys) {
    const item = value[key];
    if (typeof item === "string" && item.length > 0) return item;
  }
  return null;
}

function extractNumber(value: unknown, keys: string[]): number | null {
  if (!isRecord(value)) return null;
  for (const key of keys) {
    const item = value[key];
    if (typeof item === "number" && Number.isFinite(item) && item >= 0) return Math.floor(item);
  }
  return null;
}

function extractFiniteNumber(value: unknown, keys: string[]): number | null {
  if (!isRecord(value)) return null;
  for (const key of keys) {
    const item = value[key];
    if (typeof item === "number" && Number.isFinite(item) && item >= 0) return item;
  }
  return null;
}
