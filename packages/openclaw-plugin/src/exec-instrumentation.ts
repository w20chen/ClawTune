import {randomUUID} from "node:crypto";
import type {PluginConfig, ResourceScope, ToolBeforeRequest, ToolDecision} from "./contracts.js";
import {isRecord} from "./config.js";
import {stableDigest} from "./redaction.js";

export type ExecutionRegistrar = {
  registerExecution(payload: Parameters<ToolRegistrationFunction>[0]): Promise<{one_time_token: string}>;
};

type ToolRegistrationFunction = (payload: {
  execution_id: string;
  tool_call_id: string | null;
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
  decision: ToolDecision,
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
  if (!shouldInstrument(event, config)) return empty;
  const params = cloneRecord(isRecord(event) ? event.params ?? event.arguments ?? event.input ?? null : null);
  if (params === null || typeof params.command !== "string" || params.command.length === 0) {
    return empty;
  }
  const requestedCommand = params.command;
  const commandDigest = stableDigest(requestedCommand);
  const executionId = extractString(event, ["tool_call_id", "toolCallId", "id"]) ?? `exec-${randomUUID()}`;
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
      tool_call_id: payload.tool_call_id,
      run_id: runId,
      session_key_hash: sessionKeyHash,
      command_digest: commandDigest,
      command: requestedCommand,
      workdir: typeof params.workdir === "string" ? params.workdir : null,
      host: typeof params.host === "string" ? params.host : "gateway",
      placement: decision.placement ?? decision.placement_advice ?? null,
      profiling: decision.profiling ?? {
        mode: config.profilingMode,
        enable_cgroup: config.enableCgroup,
        enable_affinity: config.enableAffinity,
        enable_numa: config.enableNuma
      },
      backend: config.executionBackend
    });
    token = registration.one_time_token;
  } catch (error) {
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
    CLAW_TOOL_CALL_ID: payload.tool_call_id ?? "",
    CLAW_RUN_ID: runId ?? "",
    CLAW_SESSION_HASH: sessionKeyHash ?? "",
    CLAW_COMMAND_DIGEST: commandDigest
  };

  let effectiveCommand: string | null = params.command as string;
  let payloadCommand: string | null = params.command as string;

  if (config.executionBackend === "managed-wrapper") {
    if (token === null) {
      if (config.mode === "observe" || config.failOpen) return empty;
      throw new Error("execution_registration_failed");
    }
    effectiveCommand = buildLauncherCommand(config, executionId);
    params.command = effectiveCommand;
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
  const launcherInvocation = [
    ...(config.launcherInterpreter === null ? [] : [shellQuote(config.launcherInterpreter)]),
    shellQuote(config.launcherPath),
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
