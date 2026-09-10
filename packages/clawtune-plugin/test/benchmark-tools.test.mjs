import {test} from "node:test";
import assert from "node:assert/strict";
import {mkdtempSync, writeFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {registerBenchmarkTools} from "../dist/benchmark-tools.js";

test("benchmark tools are runner-scoped and forward exact native arguments", async () => {
  const previous = process.env.CLAWTUNE_BENCHMARK_TOOLS;
  const fetchBefore = globalThis.fetch;
  const root = mkdtempSync(join(tmpdir(), "clawtune-bridge-"));
  try {
    delete process.env.CLAWTUNE_BENCHMARK_TOOLS;
    registerBenchmarkTools({registerTool() {assert.fail("daily sessions must not register task tools");}});
    const path = join(root, "manifest.json");
    writeFileSync(path, JSON.stringify({schema: "clawtune.tool-bridge.v1",
      endpoint: "http://127.0.0.1:12345/call", token: "test-token",
      tools: [{name: "increment", parameters: {type: "object"}}]}));
    process.env.CLAWTUNE_BENCHMARK_TOOLS = path;
    let tool;
    registerBenchmarkTools({registerTool(value) {tool = value;}});
    globalThis.fetch = async (url, options) => {
      assert.equal(String(url), "http://127.0.0.1:12345/call");
      assert.equal(options.headers.Authorization, "Bearer test-token");
      assert.deepEqual(JSON.parse(options.body), {name: "increment", arguments: {count: 3}, call_id: "call-1"});
      return {ok: true, json: async () => ({result: 7})};
    };
    assert.deepEqual(await tool.execute("call-1", {count: 3}), {content: [{type: "text", text: "7"}]});
  } finally {
    globalThis.fetch = fetchBefore;
    if (previous === undefined) delete process.env.CLAWTUNE_BENCHMARK_TOOLS;
    else process.env.CLAWTUNE_BENCHMARK_TOOLS = previous;
    rmSync(root, {recursive: true});
  }
});
