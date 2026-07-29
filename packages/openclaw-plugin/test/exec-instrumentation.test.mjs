import test from "node:test";
import assert from "node:assert/strict";
import {instrumentExecParams} from "../dist/exec-instrumentation.js";

const baseConfig = {
  endpoint: "http://localhost:8765",
  mode: "observe",
  decisionTimeoutMs: 800,
  reportTimeoutMs: 800,
  failOpen: true,
  sendRawParams: false,
  recordRawTrace: false,
  logLevel: "info",
  executionBackend: "marker",
  launcherPath: "/opt/claw/bin/claw-launch",
  launcherInterpreter: null,
  collectorSocket: "/run/claw/collector.sock",
  instrumentHosts: ["gateway"],
  instrumentTools: ["exec"],
  enableCgroup: true,
  enableAffinity: true,
  enableNuma: true,
  profilingMode: "off",
  securityBoundaryAccepted: false
};

const payload = {
  schema_version: "scheduler.v1",
  event_id: "evt-1",
  occurred_at: "2026-07-16T00:00:00Z",
  plugin_version: "0.1.1",
  run_id: "run-1",
  session_id: null,
  session_key: "session-secret",
  agent_id: null,
  tool_call_id: "call-1",
  tool_name: "exec",
  tool_kind: null,
  tool_input_kind: null,
  operation_hint: null,
  derived_paths: [],
  params_digest: "sha256:" + "a".repeat(64),
  param_features: {
    serialized_size_bytes: 0,
    string_length: 0,
    list_item_count: 0,
    path_count: 0,
    has_command_like_field: true
  },
  raw_params: null,
  resource_scope: null
};

const decision = {
  decision_id: "decision-1",
  action: "allow",
  reason_code: "observe_only",
  reason: "ok",
  policy_name: "observe-only",
  policy_version: "1",
  lease_id: null,
  prediction: {
    duration_p50_ms: null,
    duration_p90_ms: null,
    resource_class: "unknown",
    confidence: null
  },
  placement_advice: {
    cpu_set: null,
    numa_node: null,
    llc_cluster: null,
    advisory: true
  }
};

test("marker backend injects env without changing command", async () => {
  const seen = [];
  const client = {
    async registerExecution(request) {
      seen.push(request);
      return {one_time_token: "token-1"};
    }
  };
  const event = {toolName: "exec", toolCallId: "call-1", params: {command: "pytest tests -q", env: {KEEP: "1"}}};

  const result = await instrumentExecParams(event, {}, payload, decision, client, baseConfig);

  assert.equal(result.executionId, "call-1");
  assert.equal(result.params.command, "pytest tests -q");
  assert.equal(result.params.env.KEEP, "1");
  assert.equal(result.params.env.CLAW_EXECUTION_ID, "call-1");
  assert.equal(result.params.env.CLAW_TOOL_CALL_ID, "call-1");
  assert.equal(result.params.env.CLAW_RUN_ID, "run-1");
  assert.match(result.params.env.CLAW_COMMAND_DIGEST, /^sha256:[a-f0-9]{64}$/);
  assert.match(result.params.env.CLAW_SESSION_HASH, /^sha256:[a-f0-9]{64}$/);
  assert.equal(result.params.env.CLAW_EXECUTION_TOKEN, undefined);
  assert.equal(seen[0].command, "pytest tests -q");
});

test("exec instrumentation forwards configured profiling toggles", async () => {
  const seen = [];
  const client = {
    async registerExecution(request) {
      seen.push(request);
      return {one_time_token: "token-1"};
    }
  };
  const event = {toolName: "exec", toolCallId: "call-1", params: {command: "pytest tests -q"}};

  await instrumentExecParams(
    event,
    {},
    payload,
    decision,
    client,
    {...baseConfig, enableCgroup: false, enableAffinity: false, enableNuma: false}
  );

  assert.equal(seen[0].profiling.enable_cgroup, false);
  assert.equal(seen[0].profiling.enable_affinity, false);
  assert.equal(seen[0].profiling.enable_numa, false);
});

test("exec instrumentation drops shell startup env override keys", async () => {
  const client = {
    async registerExecution() {
      return {one_time_token: "token-1"};
    }
  };
  const event = {
    toolName: "exec",
    toolCallId: "call-1",
    params: {
      command: "pytest tests -q",
      env: {KEEP: "1", BASH_ENV: "/tmp/unsafe", ENV: "/tmp/unsafe"}
    }
  };

  const result = await instrumentExecParams(event, {}, payload, decision, client, baseConfig);

  assert.equal(result.params.env.KEEP, "1");
  assert.equal("BASH_ENV" in result.params.env, false);
  assert.equal("ENV" in result.params.env, false);
});

test("exec instrumentation forwards launcher cgroup environment", async () => {
  const names = [
    "CLAW_CGROUP_ROOT",
    "CLAW_CGROUP_REQUIRED",
    "CLAW_CGROUP_DEBUG",
    "CLAW_LAUNCH_MODE",
    "CLAW_LAUNCH_DEBUG",
    "CLAW_SCHEDULER_ENDPOINT",
    "CLAW_SANDBOX_CONTAINER_ID",
    "AGENT_SCHEDULER_SANDBOX_CONTAINER_ID"
  ];
  const previous = Object.fromEntries(names.map((name) => [name, process.env[name]]));
  process.env.CLAW_CGROUP_ROOT = "/sys/fs/cgroup/claw";
  process.env.CLAW_CGROUP_REQUIRED = "1";
  process.env.CLAW_CGROUP_DEBUG = "1";
  process.env.CLAW_LAUNCH_MODE = "fork-exec";
  process.env.CLAW_LAUNCH_DEBUG = "1";
  process.env.CLAW_SCHEDULER_ENDPOINT = "http://host.docker.internal:8765";
  process.env.CLAW_SANDBOX_CONTAINER_ID = "5a423f3b2078";
  process.env.AGENT_SCHEDULER_SANDBOX_CONTAINER_ID = "5a423f3b2078";
  const client = {
    async registerExecution() {
      return {one_time_token: "token-1"};
    }
  };
  const event = {toolName: "exec", toolCallId: "call-1", params: {command: "pytest tests -q"}};

  try {
    const result = await instrumentExecParams(event, {}, payload, decision, client, baseConfig);

    assert.equal(result.params.env.CLAW_CGROUP_ROOT, "/sys/fs/cgroup/claw");
    assert.equal(result.params.env.CLAW_CGROUP_REQUIRED, "1");
    assert.equal(result.params.env.CLAW_CGROUP_DEBUG, "1");
    assert.equal(result.params.env.CLAW_LAUNCH_MODE, "fork-exec");
    assert.equal(result.params.env.CLAW_LAUNCH_DEBUG, "1");
    assert.equal(result.params.env.CLAW_SCHEDULER_ENDPOINT, "http://host.docker.internal:8765");
    assert.equal(result.params.env.CLAW_SANDBOX_CONTAINER_ID, "5a423f3b2078");
    assert.equal(result.params.env.AGENT_SCHEDULER_SANDBOX_CONTAINER_ID, "5a423f3b2078");
  } finally {
    for (const name of names) {
      if (previous[name] === undefined) {
        delete process.env[name];
      } else {
        process.env[name] = previous[name];
      }
    }
  }
});

test("exec instrumentation can force sandbox workdir from environment", async () => {
  const previous = process.env.CLAW_EXEC_WORKDIR;
  process.env.CLAW_EXEC_WORKDIR = "/workspace";
  const seen = [];
  const client = {
    async registerExecution(request) {
      seen.push(request);
      return {one_time_token: "token-1"};
    }
  };
  const event = {
    toolName: "exec",
    toolCallId: "call-1",
    params: {command: "pwd", elevated: true, host: "gateway", workdir: "/home/user/project"}
  };

  try {
    const result = await instrumentExecParams(event, {}, payload, decision, client, baseConfig);

    assert.equal(result.params.workdir, "/workspace");
    assert.equal(result.params.cwd, "/workspace");
    assert.equal("host" in result.params, false);
    assert.equal("elevated" in result.params, false);
    assert.equal(result.params.env.CLAW_EXEC_WORKDIR, "/workspace");
    assert.equal(seen[0].workdir, "/workspace");
    assert.equal(seen[0].host, "gateway");
  } finally {
    if (previous === undefined) {
      delete process.env.CLAW_EXEC_WORKDIR;
    } else {
      process.env.CLAW_EXEC_WORKDIR = previous;
    }
  }
});

test("managed-wrapper passes the claim token outside the process argv", async () => {
  const client = {
    async registerExecution() {
      return {one_time_token: "-token-1"};
    }
  };
  const event = {toolName: "exec", toolCallId: "call-1", params: {command: "echo raw-command"}};

  const result = await instrumentExecParams(
    event,
    {},
    payload,
    decision,
    client,
    {...baseConfig, executionBackend: "managed-wrapper", securityBoundaryAccepted: true}
  );

  assert.equal(result.params.command, "'/opt/claw/bin/claw-launch' run --execution-id='call-1'");
  assert.equal(result.params.command.includes("raw-command"), false);
  assert.equal(result.params.command.includes("token-1"), false);
  assert.equal(result.params.env.CLAW_EXECUTION_ID, "call-1");
  assert.equal(result.params.env.CLAW_EXECUTION_TOKEN, "-token-1");
  assert.equal(result.params.env.CLAW_SCHEDULER_ENDPOINT, "http://localhost:8765");
});

test("managed-wrapper can invoke a launcher on a noexec workspace through an interpreter", async () => {
  const client = {
    async registerExecution() {
      return {one_time_token: "token-1"};
    }
  };
  const event = {toolName: "exec", toolCallId: "call-1", params: {command: "echo raw-command"}};

  const result = await instrumentExecParams(
    event,
    {},
    payload,
    decision,
    client,
    {
      ...baseConfig,
      executionBackend: "managed-wrapper",
      securityBoundaryAccepted: true,
      launcherPath: "/workspace/.claw/bin/claw-launch",
      launcherInterpreter: "/bin/sh"
    }
  );

  assert.equal(
    result.params.command,
    "'/bin/sh' -c 'exec '\\''/bin/sh'\\'' '\\''/workspace/.claw/bin/claw-launch'\\'' run --execution-id='\\''call-1'\\'''"
  );
});
