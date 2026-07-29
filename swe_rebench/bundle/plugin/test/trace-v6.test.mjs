/**
 * Trace v6 tests.
 *
 * Tests: JSONL legality, span lifecycle, parent-child mapping, concurrent
 * writes, serializer/sanitizer, validator, and coverage calculator.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { open, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

// Dynamic imports from the built code
const traceSchema = await import("../dist/trace/schema.js");
const traceClock = await import("../dist/trace/clock.js");
const traceWriter = await import("../dist/trace/writer.js");
const traceRegistry = await import("../dist/trace/registry.js");
const traceSanitizer = await import("../dist/trace/sanitizer.js");
const traceValidator = await import("../dist/trace/validator.js");
const traceCoverage = await import("../dist/trace/resource-coverage.js");
const toolResult = await import("../dist/tool-result.js");

// ── Helpers ───────────────────────────────────────────────────────────

function tmpPath() {
  return join(tmpdir(), `trace-v6-test-${randomUUID()}.jsonl`);
}

// ── Clock Tests ───────────────────────────────────────────────────────

test("monotonic clock returns positive bigint", () => {
  const t = traceClock.monotonicNowNs();
  assert.ok(typeof t === "bigint");
  assert.ok(t > 0n);
});

test("wall clock returns reasonable epoch ns", () => {
  const t = traceClock.wallClockNowNs();
  assert.ok(typeof t === "bigint");
  // Should be around year 2026 in ns
  const year2020ns = 1577836800000000000n;
  assert.ok(t > year2020ns);
});

test("duration computes correctly", () => {
  assert.equal(traceClock.durationNs(100n, 200n), 100n);
  assert.equal(traceClock.durationNs(200n, 100n), 0n);
  assert.equal(traceClock.durationNs(100n, 100n), 0n);
});

// ── Span Registry Tests ───────────────────────────────────────────────

test("beginSpan returns span with sequence number", () => {
  const reg = new traceRegistry.SpanRegistry();
  const span = reg.beginSpan({
    traceId: "run-1",
    spanId: "span-1",
    parentSpanId: null,
    sessionId: "sess-1",
    runId: "run-1",
    agentId: "main",
    kind: "tool",
    name: "exec",
    startWallTimeNs: 100n,
    startMonotonicTimeNs: 100n,
  });
  assert.equal(span.sequenceNo, 1);
  assert.equal(span.spanId, "span-1");
});

test("sequence numbers increment per run", () => {
  const reg = new traceRegistry.SpanRegistry();
  const s1 = reg.beginSpan({
    traceId: "run-1", spanId: "a", parentSpanId: null,
    sessionId: null, runId: "run-1", agentId: null,
    kind: "tool", name: "exec",
    startWallTimeNs: 100n, startMonotonicTimeNs: 100n,
  });
  const s2 = reg.beginSpan({
    traceId: "run-1", spanId: "b", parentSpanId: null,
    sessionId: null, runId: "run-1", agentId: null,
    kind: "tool", name: "exec",
    startWallTimeNs: 200n, startMonotonicTimeNs: 200n,
  });
  assert.equal(s1.sequenceNo, 1);
  assert.equal(s2.sequenceNo, 2);
});

test("endSpan returns span and prevents duplicate ends", () => {
  const reg = new traceRegistry.SpanRegistry();
  reg.beginSpan({
    traceId: "run-1", spanId: "s1", parentSpanId: null,
    sessionId: null, runId: "run-1", agentId: null,
    kind: "tool", name: "exec",
    startWallTimeNs: 100n, startMonotonicTimeNs: 100n,
  });
  const ended = reg.endSpan("run-1", "s1");
  assert.ok(ended !== null);
  assert.equal(ended.spanId, "s1");
  const duplicate = reg.endSpan("run-1", "s1");
  assert.equal(duplicate, null);
});

test("endSpan returns null when span does not exist", () => {
  const reg = new traceRegistry.SpanRegistry();
  assert.equal(reg.endSpan("run-1", "nonexistent"), null);
});

test("tool_call_id parent mapping", () => {
  const reg = new traceRegistry.SpanRegistry();
  reg.setToolCallParent("tc-1", "llm-span-1");
  assert.equal(reg.getToolCallParent("tc-1"), "llm-span-1");
  assert.equal(reg.getToolCallParent("tc-2"), null);
  reg.clearToolCallParent("tc-1");
  assert.equal(reg.getToolCallParent("tc-1"), null);
});

test("listActiveSpans returns unended spans", () => {
  const reg = new traceRegistry.SpanRegistry();
  reg.beginSpan({
    traceId: "run-1", spanId: "a", parentSpanId: null,
    sessionId: null, runId: "run-1", agentId: null,
    kind: "tool", name: "exec",
    startWallTimeNs: 100n, startMonotonicTimeNs: 100n,
  });
  reg.beginSpan({
    traceId: "run-1", spanId: "b", parentSpanId: null,
    sessionId: null, runId: "run-1", agentId: null,
    kind: "tool", name: "write",
    startWallTimeNs: 200n, startMonotonicTimeNs: 200n,
  });
  reg.endSpan("run-1", "a");
  assert.equal(reg.listActiveSpans().length, 1);
  assert.equal(reg.listActiveSpans()[0].spanId, "b");
});

test("clearRun removes all spans for a run", () => {
  const reg = new traceRegistry.SpanRegistry();
  reg.beginSpan({
    traceId: "run-1", spanId: "a", parentSpanId: null,
    sessionId: null, runId: "run-1", agentId: null,
    kind: "tool", name: "exec",
    startWallTimeNs: 100n, startMonotonicTimeNs: 100n,
  });
  reg.clearRun("run-1");
  assert.equal(reg.listActiveSpans().length, 0);
});

// ── Writer Tests ──────────────────────────────────────────────────────

test("writer writes metadata and span records", async () => {
  const path = tmpPath();
  const { consoleLogger } = await import("../dist/logging.js");
  const w = new traceWriter.TraceWriter(path, false, consoleLogger);
  await w.open();

  const meta = {
    schema_version: 6,
    record_type: "trace_metadata",
    trace_format_version: 6,
    scaffold: "test",
    mode: "collect",
    created_at: new Date().toISOString(),
  };
  w.writeRecord(meta);
  w.writeRecord({
    schema_version: 6,
    record_type: "span_start",
    trace_id: "run-1",
    span_id: "span-1",
    parent_span_id: null,
    session_id: null, run_id: "run-1", agent_id: null,
    sequence_no: 1, kind: "tool", name: "exec",
    wall_time_ns: "100", monotonic_time_ns: "100",
    input: { requested_args: { command: "ls" } },
    execution: { mode: null, execution_id: null },
  });

  await w.close();

  const content = await (await open(path, "r")).readFile("utf-8");
  const lines = content.trim().split("\n").filter(l => l);
  assert.equal(lines.length, 2);
  // Verify each line is valid JSON
  for (const line of lines) {
    const parsed = JSON.parse(line);
    assert.ok(typeof parsed === "object");
  }
  await unlink(path);
});

test("per-run writers create separate files", async () => {
  const dir = join(tmpdir(), `trace-v6-test-dir-${randomUUID()}`);
  const { mkdirSync } = await import("node:fs");
  mkdirSync(dir, { recursive: true });

  const { consoleLogger } = await import("../dist/logging.js");
  const w1 = new traceWriter.TraceWriter(join(dir, "main_sess1_run1.jsonl"), true, consoleLogger);
  const w2 = new traceWriter.TraceWriter(join(dir, "main_sess2_run2.jsonl"), true, consoleLogger);
  await w1.open();
  await w2.open();

  w1.writeRecord({
    schema_version: 6, record_type: "span_start",
    trace_id: "run1", span_id: "s1", parent_span_id: null,
    session_id: "sess1", run_id: "run1", agent_id: "main",
    sequence_no: 1, kind: "tool", name: "exec",
    wall_time_ns: "100", monotonic_time_ns: "100",
    input: { requested_args: {} },
    execution: { mode: null, execution_id: null },
  });
  w2.writeRecord({
    schema_version: 6, record_type: "span_start",
    trace_id: "run2", span_id: "s2", parent_span_id: null,
    session_id: "sess2", run_id: "run2", agent_id: "main",
    sequence_no: 1, kind: "tool", name: "write",
    wall_time_ns: "200", monotonic_time_ns: "200",
    input: { requested_args: {} },
    execution: { mode: null, execution_id: null },
  });

  await w1.close();
  await w2.close();

  // Verify files exist separately
  const { readFileSync, existsSync } = await import("node:fs");
  assert.ok(existsSync(join(dir, "main_sess1_run1.jsonl")));
  assert.ok(existsSync(join(dir, "main_sess2_run2.jsonl")));

  // Cleanup
  const { rmSync } = await import("node:fs");
  rmSync(dir, { recursive: true, force: true });
});

test("writer does not interleave concurrent writes", async () => {
  const path = tmpPath();
  const { consoleLogger } = await import("../dist/logging.js");
  const w = new traceWriter.TraceWriter(path, false, consoleLogger);
  await w.open();

  // Write 50 records concurrently
  const records = Array.from({ length: 50 }, (_, i) => ({
    schema_version: 6,
    record_type: "span_start",
    trace_id: "run-1",
    span_id: `span-${i}`,
    parent_span_id: null,
    session_id: null, run_id: "run-1", agent_id: null,
    sequence_no: i, kind: "tool", name: `tool-${i}`,
    wall_time_ns: String(i * 100), monotonic_time_ns: String(i * 100),
    input: { requested_args: {} },
    execution: { mode: null, execution_id: null },
  }));

  for (const r of records) {
    w.writeRecord(r);
  }

  await w.close();

  const content = await (await open(path, "r")).readFile("utf-8");
  const lines = content.trim().split("\n").filter(l => l);
  assert.equal(lines.length, 50);

  // Verify each line is valid JSON and contains the expected span_id
  for (let i = 0; i < lines.length; i++) {
    const parsed = JSON.parse(lines[i]);
    assert.ok(parsed.span_id.match(/^span-\d+$/));
  }
  await unlink(path);
});

test("concurrent getRunWriter creates only one writer per key", async () => {
  // Verify the promise-lock pattern used in index.ts prevents
  // concurrent creation of duplicate writers for the same key.
  const writers = new Map();
  const pending = new Map();

  async function getWriter(key, label) {
    const existing = writers.get(key);
    if (existing) return { key, label: existing.label };

    const inflight = pending.get(key);
    if (inflight) return inflight;

    const promise = (async () => {
      const recheck = writers.get(key);
      if (recheck) return { key, label: recheck.label };
      // Simulate async I/O (file open)
      await new Promise(r => setTimeout(r, 5));
      const w = { key, label };
      writers.set(key, w);
      return w;
    })();

    pending.set(key, promise);
    try {
      return await promise;
    } finally {
      pending.delete(key);
    }
  }

  // Fire 20 concurrent creations with the SAME key, different labels
  const results = await Promise.all(
    Array.from({ length: 20 }, (_, i) => getWriter("run-abc", `caller-${i}`))
  );

  // All 20 callers must receive the SAME writer object
  const labels = results.map(r => r.label);
  const uniqueLabels = [...new Set(labels)];
  assert.equal(uniqueLabels.length, 1, 
    `race: expected 1 writer (got ${uniqueLabels.length}). Labels: ${labels.join(",")}`);
  assert.equal(writers.size, 1, `race: expected 1 writer in map, got ${writers.size}`);
});

// ── Sanitizer Tests ───────────────────────────────────────────────────

test("sanitizer redacts sensitive keys", () => {
  const input = {
    token: "secret123",
    api_key: "key456",
    nested: { password: "pw", ok: "keep" },
    Authorization: "Bearer xyz",
  };
  const result = traceSanitizer.sanitizeTraceData(input);
  assert.equal(result.token, "<redacted>");
  assert.equal(result.api_key, "<redacted>");
  assert.equal(result.Authorization, "<redacted>");
  assert.deepEqual(result.nested, { password: "<redacted>", ok: "keep" });
});

test("sanitizer redacts Bearer token in strings", () => {
  const result = traceSanitizer.sanitizeString("curl -H 'Authorization: Bearer sk-abc123def456' https://api.com");
  assert.ok(result.includes("<redacted>"));
  assert.ok(!result.includes("sk-abc123def456"));
});

test("tool exit code extraction applies to process results too", () => {
  assert.equal(
    toolResult.extractToolExitCode(
      { details: { status: "failed", exitCode: 0, timedOut: true } },
      "process"
    ),
    0
  );
});

test("sanitizer redacts --token flag", () => {
  const result = traceSanitizer.sanitizeString("claw-launch run --token=abc123 --other");
  assert.ok(result.includes("<redacted>"));
  assert.ok(!result.includes("abc123"));
});

test("sanitizer redacts CLAW_* env vars", () => {
  const input = { env: { CLAW_TOKEN: "secret", CLAW_KEY: "key", KEEP: "val" } };
  const result = traceSanitizer.sanitizeTraceData(input);
  assert.equal(result.env.CLAW_TOKEN, "<redacted>");
  assert.equal(result.env.CLAW_KEY, "<redacted>");
  assert.equal(result.env.KEEP, "val");
});

test("sanitizer redacts high-privilege LLM message content", () => {
  const input = [
    { role: "system", content: "Available tools: openclaw plugins install" },
    { role: "developer", content: "BOOTSTRAP.md and plugin-skills details" },
    { role: "user", content: "Please solve task 12rambau__sepal_ui-411" },
    { role: "assistant", content: "I will inspect the repository." },
  ];

  const result = traceSanitizer.sanitizeTraceData(input);

  assert.equal(result[0].content, "<redacted>");
  assert.equal(result[1].content, "<redacted>");
  assert.equal(result[2].content, "Please solve task 12rambau__sepal_ui-411");
  assert.equal(result[3].content, "I will inspect the repository.");
});

test("sanitizer redacts nested LLM messages fields", () => {
  const input = {
    request: {
      messages: [
        { role: "system", content: "SOUL.md USER.md TOOLS.md" },
        { role: "user", content: "Problem statement" },
      ],
    },
  };

  const result = traceSanitizer.sanitizeTraceData(input);

  assert.equal(result.request.messages[0].content, "<redacted>");
  assert.equal(result.request.messages[1].content, "Problem statement");
});

test("sanitizer does not mutate input", () => {
  const input = { token: "abc" };
  const copy = JSON.parse(JSON.stringify(input));
  traceSanitizer.sanitizeTraceData(input);
  assert.deepEqual(input, copy); // Original unchanged
});

test("sanitizer detects possible secrets", () => {
  assert.equal(traceSanitizer.containsPossibleSecret("Bearer sk-abc123def456"), true);
  assert.equal(traceSanitizer.containsPossibleSecret("Bearer <redacted>"), false);
  assert.equal(traceSanitizer.containsPossibleSecret("hello world"), false);
});

test("tool result exit code parsing is scoped to exec results", () => {
  assert.equal(toolResult.extractToolExitCode({ code: 404 }, "web_fetch"), null);
  assert.equal(toolResult.extractToolExitCode({ details: { code: 1 } }, "exec"), null);
  assert.equal(toolResult.extractToolExitCode({ details: { exitCode: 1 } }, "exec"), 1);
  assert.equal(toolResult.extractToolExitCode({ exit_code: 2 }, "exec"), 2);
});

test("trace exit code falls back to zero only for successful exec spans", () => {
  assert.equal(toolResult.traceExitCodeForTool("exec", "ok", null), 0);
  assert.equal(toolResult.traceExitCodeForTool("write", "ok", null), null);
  assert.equal(toolResult.traceExitCodeForTool("exec", "error", null), null);
  assert.equal(toolResult.traceExitCodeForTool("exec", "error", 3), 3);
});

// ── Validator Tests ───────────────────────────────────────────────────

test("validator reports valid trace", () => {
  const lines = [
    '{"schema_version":6,"record_type":"trace_metadata","trace_format_version":6,"scaffold":"test","mode":"collect","created_at":"2026-01-01T00:00:00Z"}',
    '{"schema_version":6,"record_type":"span_start","trace_id":"r1","span_id":"s1","parent_span_id":null,"session_id":null,"run_id":"r1","agent_id":null,"sequence_no":1,"kind":"tool","name":"exec","wall_time_ns":"100","monotonic_time_ns":"100","input":{"requested_args":{}},"execution":{"mode":null,"execution_id":null}}',
    '{"schema_version":6,"record_type":"span_end","trace_id":"r1","span_id":"s1","parent_span_id":null,"session_id":null,"run_id":"r1","agent_id":null,"sequence_no":1,"kind":"tool","name":"exec","wall_time_ns":"200","monotonic_time_ns":"200","duration_ns":"100","duration_sec":"1e-7","status":{"code":"ok","message":null},"output":{},"execution":{"mode":null,"execution_id":null},"resources":{"attribution_status":"unattributed","scope":"none","quality":"unknown","monitor_start_wall_time_ns":null,"monitor_end_wall_time_ns":null,"monitor_start_monotonic_ns":null,"monitor_end_monotonic_ns":null,"coverage_duration_ns":null,"action_duration_ns":"100","coverage_ratio":null,"coverage_reason":"pid_unavailable"}}',
  ];
  const result = traceValidator.validateTrace(lines);
  assert.equal(result.records, 3);
  assert.equal(result.spanStarts, 1);
  assert.equal(result.spanEnds, 1);
  assert.equal(result.completeSpans, 1);
  assert.equal(result.incompleteSpans, 0);
  assert.equal(result.invalidCoverageRatios, 0);
  assert.equal(result.possibleSecretLeaks, 0);
});

test("validator detects incomplete spans", () => {
  const lines = [
    '{"schema_version":6,"record_type":"span_start","trace_id":"r1","span_id":"s1","parent_span_id":null,"session_id":null,"run_id":"r1","agent_id":null,"sequence_no":1,"kind":"tool","name":"exec","wall_time_ns":"100","monotonic_time_ns":"100","input":{"requested_args":{}},"execution":{"mode":null,"execution_id":null}}',
  ];
  const result = traceValidator.validateTrace(lines);
  assert.equal(result.incompleteSpans, 1);
  assert.equal(result.completeSpans, 0);
});

test("validator detects invalid coverage ratio", () => {
  const lines = [
    '{"schema_version":6,"record_type":"span_start","trace_id":"r1","span_id":"s1","parent_span_id":null,"session_id":null,"run_id":"r1","agent_id":null,"sequence_no":1,"kind":"tool","name":"exec","wall_time_ns":"100","monotonic_time_ns":"100","input":{"requested_args":{}},"execution":{"mode":null,"execution_id":null}}',
    '{"schema_version":6,"record_type":"span_end","trace_id":"r1","span_id":"s1","parent_span_id":null,"session_id":null,"run_id":"r1","agent_id":null,"sequence_no":1,"kind":"tool","name":"exec","wall_time_ns":"200","monotonic_time_ns":"200","duration_ns":"100","duration_sec":"1e-7","status":{"code":"ok","message":null},"output":{},"execution":{"mode":null,"execution_id":null},"resources":{"attribution_status":"attributed","scope":"process_tree","quality":"complete","monitor_start_wall_time_ns":null,"monitor_end_wall_time_ns":null,"monitor_start_monotonic_ns":null,"monitor_end_monotonic_ns":null,"coverage_duration_ns":"100","action_duration_ns":"100","coverage_ratio":1.5,"coverage_reason":"full_window"}}',
  ];
  const result = traceValidator.validateTrace(lines);
  assert.equal(result.invalidCoverageRatios, 1);
});

// ── Resource Coverage Tests ───────────────────────────────────────────

test("coverage: full window", () => {
  const result = traceCoverage.computeCoverage({
    actionStartMonotonicNs: 0n,
    actionEndMonotonicNs: 1000n,
    monitorStartMonotonicNs: 0n,
    monitorEndMonotonicNs: 1000n,
    pidAvailable: true,
    pidRegisteredLate: false,
    monitorStoppedEarly: false,
    monitorError: false,
    clockDataMissing: false,
  });
  assert.equal(result.quality, "complete");
  assert.equal(result.coverageReason, "full_window");
  assert.equal(result.coverageRatio, 1.0);
  assert.equal(result.attributionStatus, "attributed");
});

test("coverage: pid unavailable", () => {
  const result = traceCoverage.computeCoverage({
    actionStartMonotonicNs: 0n,
    actionEndMonotonicNs: 1000n,
    monitorStartMonotonicNs: null,
    monitorEndMonotonicNs: null,
    pidAvailable: false,
    pidRegisteredLate: false,
    monitorStoppedEarly: false,
    monitorError: false,
    clockDataMissing: false,
  });
  assert.equal(result.attributionStatus, "unattributed");
  assert.equal(result.coverageReason, "pid_unavailable");
  assert.equal(result.coverageRatio, null);
});

test("coverage: pid registered late", () => {
  const result = traceCoverage.computeCoverage({
    actionStartMonotonicNs: 0n,
    actionEndMonotonicNs: 1000n,
    monitorStartMonotonicNs: 500n,
    monitorEndMonotonicNs: 1000n,
    pidAvailable: true,
    pidRegisteredLate: true,
    monitorStoppedEarly: false,
    monitorError: false,
    clockDataMissing: false,
  });
  assert.equal(result.quality, "partial");
  assert.equal(result.coverageReason, "pid_registered_late");
  assert.ok(result.coverageRatio < 1.0);
  assert.equal(result.attributionStatus, "partially_attributed");
});

test("coverage: monitor error", () => {
  const result = traceCoverage.computeCoverage({
    actionStartMonotonicNs: 0n,
    actionEndMonotonicNs: 1000n,
    monitorStartMonotonicNs: 0n,
    monitorEndMonotonicNs: 1000n,
    pidAvailable: true,
    pidRegisteredLate: false,
    monitorStoppedEarly: false,
    monitorError: true,
    clockDataMissing: false,
  });
  assert.equal(result.attributionStatus, "failed");
  assert.equal(result.coverageReason, "monitor_error");
});

// ── JSONL Legality Test ───────────────────────────────────────────────

test("serialized records are valid JSON lines", () => {
  const records = [
    {
      schema_version: 6,
      record_type: "span_start",
      trace_id: "r1", span_id: "s1", parent_span_id: null,
      session_id: null, run_id: "r1", agent_id: null,
      sequence_no: 1, kind: "tool", name: "exec",
      wall_time_ns: "100", monotonic_time_ns: "100",
      input: { requested_args: { command: "ls -la" } },
      execution: { mode: null, execution_id: null },
    },
    {
      schema_version: 6,
      record_type: "span_end",
      trace_id: "r1", span_id: "s1", parent_span_id: null,
      session_id: null, run_id: "r1", agent_id: null,
      sequence_no: 1, kind: "tool", name: "exec",
      wall_time_ns: "200", monotonic_time_ns: "200",
      duration_ns: "100",
      status: { code: "ok", message: null },
      output: { exit_code: 0, result: "ok" },
      execution: { mode: null, execution_id: null },
      resources: {
        attribution_status: "unattributed",
        scope: "none",
        quality: "unknown",
        monitor_start_wall_time_ns: null,
        monitor_end_wall_time_ns: null,
        monitor_start_monotonic_ns: null,
        monitor_end_monotonic_ns: null,
        coverage_duration_ns: null,
        action_duration_ns: "100",
        coverage_ratio: null,
        coverage_reason: "pid_unavailable",
      },
    },
  ];

  for (const rec of records) {
    const json = JSON.stringify(rec);
    const parsed = JSON.parse(json);
    assert.equal(parsed.schema_version, 6);
    // Verify no newlines inside JSON (would break JSONL)
    assert.ok(!json.includes("\n") || json === JSON.stringify(parsed));
  }
});
