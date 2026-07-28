import test from "node:test";
import assert from "node:assert/strict";
import {loadConfig} from "../dist/config.js";

test("loadConfig uses managed-wrapper as the default exec path", () => {
  const config = loadConfig({});

  assert.equal(config.executionBackend, "managed-wrapper");
  assert.equal(config.securityBoundaryAccepted, true);
});

test("loadConfig deep-merges partial trace config", () => {
  const config = loadConfig({
    trace: {
      trace_dir: "/tmp/openclaw-traces",
    },
  });

  assert.equal(config.trace.trace_dir, "/tmp/openclaw-traces");
  assert.equal(config.trace.schema_version, 6);
  assert.equal(config.trace.include_llm_messages, true);
  assert.equal(config.trace.include_tool_outputs, true);
});

test("loadConfig maps legacy recordRawTrace to trace capture switches", () => {
  const config = loadConfig({
    recordRawTrace: true,
  });

  assert.equal(config.recordRawTrace, true);
  assert.equal(config.trace.include_raw_events, true);
  assert.equal(config.trace.include_llm_messages, true);
  assert.equal(config.trace.include_tool_outputs, true);
});

test("loadConfig keeps execution placement toggles configurable", () => {
  const config = loadConfig({
    enableCgroup: false,
    enableAffinity: false,
    enableNuma: false,
  });

  assert.equal(config.enableCgroup, false);
  assert.equal(config.enableAffinity, false);
  assert.equal(config.enableNuma, false);
});

test("loadConfig rejects managed-wrapper placeholder launcherPath", () => {
  assert.throws(
    () => loadConfig({
      executionBackend: "managed-wrapper",
      launcherPath: "/absolute/path/to/claw-launch",
      securityBoundaryAccepted: true,
    }),
    /launcherPath is still a placeholder/
  );
});

test("loadConfig rejects managed-wrapper relative launcherPath", () => {
  assert.throws(
    () => loadConfig({
      executionBackend: "managed-wrapper",
      launcherPath: "claw-launch",
      securityBoundaryAccepted: true,
    }),
    /launcherPath must be an absolute path/
  );
});

test("loadConfig reads agent scheduler environment overrides", () => {
  const previousEndpoint = process.env.OPENCLAW_AGENT_SCHEDULER_ENDPOINT;
  const previousTraceDir = process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR;
  const previousPluginTraceDir = process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR;
  process.env.OPENCLAW_AGENT_SCHEDULER_ENDPOINT = "http://127.0.0.1:9999";
  process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR = "/tmp/agent-scheduler-traces";
  process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR = "/tmp/plugin-traces";

  try {
    const config = loadConfig({});

    assert.equal(config.endpoint, "http://127.0.0.1:9999");
    assert.equal(config.trace.trace_dir, "/tmp/plugin-traces");
  } finally {
    if (previousEndpoint === undefined) {
      delete process.env.OPENCLAW_AGENT_SCHEDULER_ENDPOINT;
    } else {
      process.env.OPENCLAW_AGENT_SCHEDULER_ENDPOINT = previousEndpoint;
    }
    if (previousTraceDir === undefined) {
      delete process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR;
    } else {
      process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR = previousTraceDir;
    }
    if (previousPluginTraceDir === undefined) {
      delete process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR;
    } else {
      process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR = previousPluginTraceDir;
    }
  }
});

test("loadConfig validates launcherInterpreter when configured", () => {
  assert.equal(loadConfig({launcherInterpreter: "/bin/sh"}).launcherInterpreter, "/bin/sh");
  assert.throws(
    () => loadConfig({launcherInterpreter: "sh"}),
    /launcherInterpreter must be an absolute path/
  );
});

test("loadConfig does not treat sidecar trace dir env as plugin trace output", () => {
  const previousTraceDir = process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR;
  const previousPluginTraceDir = process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR;
  process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR = "/tmp/sidecar-traces";
  delete process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR;

  try {
    const config = loadConfig({});

    assert.equal(config.trace.trace_dir, "");
  } finally {
    if (previousTraceDir === undefined) {
      delete process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR;
    } else {
      process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR = previousTraceDir;
    }
    if (previousPluginTraceDir === undefined) {
      delete process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR;
    } else {
      process.env.OPENCLAW_AGENT_SCHEDULER_PLUGIN_TRACE_DIR = previousPluginTraceDir;
    }
  }
});
