import test from "node:test";
import assert from "node:assert/strict";
import {loadConfig} from "../dist/config.js";

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

test("loadConfig reads agent scheduler environment overrides", () => {
  const previousEndpoint = process.env.OPENCLAW_AGENT_SCHEDULER_ENDPOINT;
  const previousTraceDir = process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR;
  process.env.OPENCLAW_AGENT_SCHEDULER_ENDPOINT = "http://127.0.0.1:9999";
  process.env.OPENCLAW_AGENT_SCHEDULER_TRACE_DIR = "/tmp/agent-scheduler-traces";

  try {
    const config = loadConfig({});

    assert.equal(config.endpoint, "http://127.0.0.1:9999");
    assert.equal(config.trace.trace_dir, "/tmp/agent-scheduler-traces");
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
  }
});
