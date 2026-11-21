import assert from "node:assert/strict";
import test from "node:test";
import {register} from "node:module";
import {readFileSync, readdirSync} from "node:fs";
import Ajv from "ajv/dist/2020.js";
import {SessionObservations} from "../dist/session-observations.js";

const owner = {gatewayId: "g", runtimeId: "runtime", sessionId: "s", sessionKey: "key",
  runId: "r", agentId: "agent"};
const model = (runId = "r", callId = "m") => ({runId, callId});
const message = (id, tools, extra = {}) => ({sessionKey: "key", messageId: id,
  message: {role: "assistant", content: tools.map(id => ({type: "toolCall", id, name: "exec"})), ...extra}});
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(foreground = true, preview) {
  const sent = [];
  let transcript, lifecycle;
  const observer = new SessionObservations((event, context) => ({...owner,
    runId: event?.runId ?? context?.runId ?? owner.runId}),
    async value => { sent.push(value); }, {
      onAgentEvent(fn) { lifecycle = fn; return () => {}; },
      onSessionTranscriptUpdate(fn) { transcript = fn; return () => {}; },
    }, foreground, preview);
  return {observer, sent, transcript: e => transcript(e), lifecycle: e => lifecycle(e)};
}

test("native transcript builds full batch; hooks settle logical calls including background", async () => {
  const {observer, sent, transcript} = fixture();
  try {
    observer.model(model(), {}, true);
    transcript(message("assistant-1", ["a", "b"]));
    observer.model(model(), {}, false); // late model-ended must not erase the batch
    observer.beforeTool({toolName: "exec", toolCallId: "a"}, {});
    observer.afterTool({toolName: "exec", toolCallId: "a", result: {details: {status: "running"}}}, {});
    await flush();
    const snapshot = sent.at(-1).payload;
    assert.equal(snapshot.batch.batch_id, "r:assistant-1");
    assert.equal(snapshot.batch.execution_mode, "parallel");
    assert.equal(snapshot.batch.mode_source, "openclaw_default_assumption");
    assert.deepEqual(snapshot.batch.tools, [
      {tool_call_id: "a", started: true, foreground: true, settled: true,
        predicted_duration_ms: null, prediction_source: null},
      {tool_call_id: "b", started: false, foreground: false, settled: false,
        predicted_duration_ms: null, prediction_source: null},
    ]);
    assert.equal(snapshot.model_call_id, null);
    assert.ok(!JSON.stringify(snapshot).includes("details"));
  } finally { await observer.close(); }
});

test("batch preview records Lattice then Tool fallback results and fences stale results", async () => {
  const pending = new Map();
  const {observer, sent, transcript} = fixture(true, call => new Promise(resolve => {
    pending.set(call.id, resolve);
  }));
  try {
    observer.model(model(), {}, true);
    transcript(message("assistant-1", ["a", "b"]));
    observer.beforeTool({toolName: "exec", toolCallId: "a"}, {});
    observer.prediction({toolCallId: "a"}, {}, {durationMs: 1500, source: "tool"});
    pending.get("a")({durationMs: 1200, source: "lattice"});
    pending.get("b")({durationMs: 800, source: "tool"});
    await flush(); await flush();
    assert.deepEqual(sent.at(-1).payload.batch.tools.map(t => t.predicted_duration_ms), [1500, 800]);
    assert.deepEqual(sent.at(-1).payload.batch.tools.map(t => t.prediction_source), ["tool", "tool"]);

    transcript(message("assistant-2", ["c"]));
    assert.equal(sent.at(-1).payload.batch.batch_id, "r:assistant-2");
  } finally { await observer.close(); }
});

test("OpenClaw transcript mirrors do not clear an active batch", async () => {
  const {observer, sent, transcript} = fixture();
  try {
    observer.model(model(), {}, true);
    transcript(message("assistant-1", ["a"]));
    transcript({sessionKey: "key", messageId: "mirror", message: {role: "assistant",
      provider: "openclaw", model: "delivery-mirror", content: []}});
    transcript({sessionKey: "key", messageId: "injected", message: {role: "assistant",
      provider: "openclaw", model: "gateway-injected", content: []}});
    await flush();
    assert.equal(sent.at(-1).payload.batch.batch_id, "r:assistant-1");
  } finally { await observer.close(); }
});

test("observed overlap and succession revise the default execution-mode assumption", async () => {
  const {observer, sent, transcript} = fixture();
  try {
    observer.model(model(), {}, true);
    transcript(message("parallel", ["a", "b"]));
    observer.beforeTool({toolName: "exec", toolCallId: "a"}, {});
    observer.beforeTool({toolName: "exec", toolCallId: "b"}, {});
    await flush();
    assert.equal(sent.at(-1).payload.batch.mode_source, "observed_overlap");

    transcript(message("sequential", ["c", "d"]));
    observer.beforeTool({toolName: "exec", toolCallId: "c"}, {});
    observer.afterTool({toolName: "exec", toolCallId: "c", result: {}}, {});
    observer.beforeTool({toolName: "exec", toolCallId: "d"}, {});
    await flush();
    assert.equal(sent.at(-1).payload.batch.execution_mode, "sequential");
    assert.equal(sent.at(-1).payload.batch.mode_source, "observed_sequence");
  } finally { await observer.close(); }
});

test("unknown background policy never turns process progress into logical ETA", async () => {
  const {observer, sent, transcript} = fixture(false);
  try {
    observer.model(model(), {}, true);
    transcript(message("assistant-1", ["a"]));
    observer.beforeTool({toolName: "exec", toolCallId: "a"}, {});
    await flush();
    assert.equal(sent.at(-1).payload.batch.tools[0].foreground, false);
  } finally { await observer.close(); }
});

test("failed yield stays active; successful yield survives final lifecycle; stale runs fenced", async () => {
  const {observer, sent, transcript, lifecycle} = fixture();
  try {
    observer.model(model(), {}, true);
    transcript(message("assistant-1", ["yield"]));
    observer.afterTool({toolName: "sessions_yield", toolCallId: "yield",
      result: {details: {status: "error"}}}, {});
    await flush();
    assert.equal(sent.at(-1).payload.run_status, "active");
    transcript(message("assistant-2", ["yield2"]));
    observer.afterTool({toolName: "sessions_yield", toolCallId: "yield2",
      result: {details: {status: "yielded"}}}, {});
    lifecycle({stream: "lifecycle", runId: "r", data: {phase: "end"}});
    await flush();
    assert.equal(sent.at(-1).payload.run_status, "yielded");
    observer.model(model("new", "new-model"), {}, true);
    lifecycle({stream: "lifecycle", runId: "r", data: {phase: "error"}});
    observer.model(model("r"), {}, true);
    await flush();
    assert.equal(sent.at(-1).identity.run_id, "new");
    assert.equal(sent.at(-1).payload.model_call_id, "new-model");
  } finally { await observer.close(); }
});

test("attempt finishing allows same-run retry; real terminal blocks late callbacks", async () => {
  const {observer, sent, lifecycle} = fixture();
  try {
    observer.model(model(), {}, true);
    lifecycle({stream: "lifecycle", runId: "r", data: {phase: "finishing"}});
    observer.model(model("r", "retry"), {}, true);
    await flush();
    assert.equal(sent.at(-1).payload.model_call_id, "retry");
    observer.model(model("r", "m"), {}, false);
    lifecycle({stream: "lifecycle", runId: "r", data: {phase: "end", aborted: true}});
    observer.model(model("r", "late"), {}, true);
    await flush();
    assert.equal(sent.at(-1).payload.run_status, "cancelled");
  } finally { await observer.close(); }
});

test("parent association can arrive late, and duplicate transcript cannot reopen a batch", async () => {
  const {observer, sent, transcript} = fixture();
  try {
    observer.model(model(), {}, true);
    transcript(message("assistant-1", ["a"]));
    observer.afterTool({toolName: "exec", toolCallId: "a"}, {});
    transcript(message("assistant-1", ["a"]));
    observer.spawned({childSessionKey: "key"}, {requesterSessionKey: "parent"});
    await flush();
    assert.equal(sent.at(-1).payload.parent_session_key, "parent");
    assert.equal(sent.at(-1).payload.batch.tools[0].settled, true);
    transcript(message("aborted-message", ["b"], {stopReason: "aborted"}));
    await flush();
    assert.equal(sent.at(-1).payload.batch.tools[0].tool_call_id, "a");
  } finally { await observer.close(); }
});

test("registered hooks and runtime subscriptions emit schema-valid complete snapshots", async () => {
  register(`data:text/javascript,${encodeURIComponent(`
    export async function resolve(specifier, context, nextResolve) {
      if (specifier === 'openclaw/plugin-sdk/plugin-entry') {
        return {url: 'data:text/javascript,export const definePluginEntry = entry => entry', shortCircuit: true};
      }
      return nextResolve(specifier, context);
    }
  `)}`, import.meta.url);
  const {default: plugin} = await import("../dist/index.js");
  const ajv = new Ajv({strict: false, validateFormats: false});
  const contracts = new URL("../../../contracts/", import.meta.url);
  for (const name of readdirSync(contracts).filter(n => n.endsWith(".schema.json"))) {
    ajv.addSchema(JSON.parse(readFileSync(new URL(name, contracts), "utf8")));
  }
  const validate = ajv.getSchema("https://openclaw.ai/schemas/clawtune/signal.schema.json");
  const hooks = new Map(), sent = [];
  let transcript, lifecycle;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    if (body.type === "session.observation") sent.push(body);
    return new Response(JSON.stringify({stored: true}), {status: 200});
  };
  try {
    plugin.register({pluginConfig: {autoStartSidecar: false, trace: {trace_dir: ""}},
      runtime: {events: {
        onAgentEvent(fn) { lifecycle = fn; return () => {}; },
        onSessionTranscriptUpdate(fn) { transcript = fn; return () => {}; },
      }},
      on(name, callback) { hooks.set(name, callback); },
      logger: {info() {}, warn() {}, error() {}}});
    const context = {sessionId: "session-real", sessionKey: "key", runId: "run-real", agentId: "a"};
    await hooks.get("model_call_started")({callId: "model-real", model: "test"}, context);
    transcript(message("turn-real", ["a", "b"]));
    await hooks.get("after_tool_call")({toolName: "read", toolCallId: "a", durationMs: 10}, context);
    await flush();
    assert.equal(sent.at(-1).identity.session_id, context.sessionId);
    assert.deepEqual(sent.at(-1).payload.batch.tools.map(t => t.settled), [true, false]);
    // Runtime lifecycle often carries only the key: it must resolve to the same concrete owner.
    lifecycle({stream: "lifecycle", sessionKey: "key", runId: "run-real", data: {phase: "end"}});
    await flush();
    assert.equal(sent.at(-1).payload.run_status, "ended");
    assert.equal(new Set(sent.map(s => s.identity.runtime_id)).size, 1);
    for (const snapshot of sent) assert.ok(validate(snapshot), JSON.stringify(validate.errors));
  } finally { globalThis.fetch = originalFetch; }
});
