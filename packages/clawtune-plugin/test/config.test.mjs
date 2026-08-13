import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {loadConfig} from "../dist/config.js";

const manifest = JSON.parse(
  readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8")
);

test("loadConfig uses managed-wrapper as the default exec path", () => {
  const config = loadConfig({});

  assert.equal(config.executionBackend, "managed-wrapper");
  assert.equal(config.securityBoundaryAccepted, true);
  assert.equal(config.autoStartSidecar, false);
  assert.equal(config.sidecarStartupTimeoutMs, 60_000);
});

test("loadConfig validates the configurable sidecar cold-start timeout", () => {
  assert.equal(
    loadConfig({sidecarStartupTimeoutMs: 90_000}).sidecarStartupTimeoutMs,
    90_000,
  );
  assert.throws(
    () => loadConfig({sidecarStartupTimeoutMs: 999}),
    /sidecarStartupTimeoutMs must be an integer between 1000 and 600000/,
  );
  assert.equal(
    manifest.configSchema.properties.sidecarStartupTimeoutMs.default,
    60_000,
  );
});

test("loadConfig validates explicit sidecar launcher settings", () => {
  assert.equal(loadConfig({autoStartSidecar: true}).autoStartSidecar, true);
  assert.equal(loadConfig({sidecarCommand: "python -m clawtune_sidecar.main"}).sidecarCommand,
    "python -m clawtune_sidecar.main");
  assert.throws(
    () => loadConfig({autoStartSidecar: "yes"}),
    /autoStartSidecar must be a boolean/
  );
  assert.throws(
    () => loadConfig({sidecarCommand: false}),
    /sidecarCommand must be a string/
  );
});

test("runtime and manifest agree that privileged eBPF sidecar auto-start is opt-in", () => {
  assert.equal(loadConfig({}).autoStartSidecar, false);
  assert.equal(
    manifest.configSchema.properties.autoStartSidecar.default,
    false
  );
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

test("loadConfig reads trace capture switches", () => {
  const config = loadConfig({
    trace: {
      include_raw_events: true,
      include_llm_messages: true,
      include_tool_outputs: true,
    },
  });

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
      launcherPath: "/absolute/path/to/clawtune-launch",
      securityBoundaryAccepted: true,
    }),
    /launcherPath is still a placeholder/
  );
});

test("loadConfig rejects managed-wrapper relative launcherPath", () => {
  assert.throws(
    () => loadConfig({
      executionBackend: "managed-wrapper",
      launcherPath: "clawtune-launch",
      securityBoundaryAccepted: true,
    }),
    /launcherPath must be an absolute path/
  );
});

test("loadConfig reads ClawTune environment overrides", () => {
  const previousEndpoint = process.env.CLAWTUNE_ENDPOINT;
  const previousTraceDir = process.env.CLAWTUNE_TRACE_DIR;
  const previousPluginTraceDir = process.env.CLAWTUNE_PLUGIN_TRACE_DIR;
  process.env.CLAWTUNE_ENDPOINT = "http://127.0.0.1:9999";
  process.env.CLAWTUNE_TRACE_DIR = "/tmp/clawtune-traces";
  process.env.CLAWTUNE_PLUGIN_TRACE_DIR = "/tmp/plugin-traces";

  try {
    const config = loadConfig({});

    assert.equal(config.endpoint, "http://127.0.0.1:9999");
    assert.equal(config.trace.trace_dir, "/tmp/plugin-traces");
  } finally {
    if (previousEndpoint === undefined) {
      delete process.env.CLAWTUNE_ENDPOINT;
    } else {
      process.env.CLAWTUNE_ENDPOINT = previousEndpoint;
    }
    if (previousTraceDir === undefined) {
      delete process.env.CLAWTUNE_TRACE_DIR;
    } else {
      process.env.CLAWTUNE_TRACE_DIR = previousTraceDir;
    }
    if (previousPluginTraceDir === undefined) {
      delete process.env.CLAWTUNE_PLUGIN_TRACE_DIR;
    } else {
      process.env.CLAWTUNE_PLUGIN_TRACE_DIR = previousPluginTraceDir;
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
  const previousTraceDir = process.env.CLAWTUNE_TRACE_DIR;
  const previousPluginTraceDir = process.env.CLAWTUNE_PLUGIN_TRACE_DIR;
  process.env.CLAWTUNE_TRACE_DIR = "/tmp/sidecar-traces";
  delete process.env.CLAWTUNE_PLUGIN_TRACE_DIR;

  try {
    const config = loadConfig({});

    assert.equal(config.trace.trace_dir, "");
  } finally {
    if (previousTraceDir === undefined) {
      delete process.env.CLAWTUNE_TRACE_DIR;
    } else {
      process.env.CLAWTUNE_TRACE_DIR = previousTraceDir;
    }
    if (previousPluginTraceDir === undefined) {
      delete process.env.CLAWTUNE_PLUGIN_TRACE_DIR;
    } else {
      process.env.CLAWTUNE_PLUGIN_TRACE_DIR = previousPluginTraceDir;
    }
  }
});
