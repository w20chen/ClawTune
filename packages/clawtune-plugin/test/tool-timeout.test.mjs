import assert from "node:assert/strict";
import test from "node:test";
import {register} from "node:module";
import {extractToolTimeout} from "../dist/tool-result.js";

test("effective OpenClaw timeout is retained without treating stdout as an outcome", () => {
  const content = [{type: "text", text: "partial output\nCommand timed out after 2.5 seconds. If this command is expected to take longer..."}];
  assert.deepEqual(extractToolTimeout({}, {details: {exitReason: "overall-timeout"}, content}),
    {confirmed_timeout: true, timeout_duration_lower_bound_ms: 2500});
  assert.deepEqual(extractToolTimeout({}, {content}),
    {confirmed_timeout: false, timeout_duration_lower_bound_ms: null});
});

test("timeout metadata preserves actual budget and does not invent one for cancellation", () => {
  assert.deepEqual(extractToolTimeout({error_type: "TimeoutError", timeout_ms: 80}, {}),
    {confirmed_timeout: true, timeout_duration_lower_bound_ms: 80});
  assert.deepEqual(extractToolTimeout({error_type: "CancelledError", timeout_ms: 80}, {}),
    {confirmed_timeout: false, timeout_duration_lower_bound_ms: null});
  assert.deepEqual(extractToolTimeout({}, {details: {exitReason: "no-output-timeout"}}),
    {confirmed_timeout: true, timeout_duration_lower_bound_ms: null});
});

test("real completion hook retains timeout when tool output recording is disabled", async () => {
  register(`data:text/javascript,${encodeURIComponent(`
    export async function resolve(specifier, context, nextResolve) {
      if (specifier === 'openclaw/plugin-sdk/plugin-entry') {
        return {url: 'data:text/javascript,export const definePluginEntry = entry => entry', shortCircuit: true};
      }
      return nextResolve(specifier, context);
    }
  `)}`, import.meta.url);
  const {default: plugin} = await import("../dist/index.js");
  const hooks = new Map();
  const requests = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    requests.push({url: String(url), body: options?.body ? JSON.parse(options.body) : null});
    return new Response(JSON.stringify({stored: true}), {status: 200});
  };
  try {
    plugin.register({pluginConfig: {autoStartSidecar: false, trace: {include_tool_outputs: false, trace_dir: ""}},
      on(name, callback) { hooks.set(name, callback); },
      logger: {info() {}, warn() {}, error() {}}});
    await hooks.get("after_tool_call")({toolName: "exec", toolCallId: "timeout-test", durationMs: 2800,
      result: {details: {exitReason: "overall-timeout", exitCode: 0},
        content: [{type: "text", text: "Command timed out after 2.5 seconds. More details."}]}}, {});
    const event = requests.find(row => row.url.endsWith("/v1/events/tool-completed")).body;
    assert.equal(event.raw_result, null);
    assert.equal(event.confirmed_timeout, true);
    assert.equal(event.timeout_duration_lower_bound_ms, 2500);
    assert.equal(event.succeeded, false);
    assert.equal(event.error_type, "timeout");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
