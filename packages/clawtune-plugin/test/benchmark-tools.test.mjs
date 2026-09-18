import {test} from "node:test";
import assert from "node:assert/strict";
import {mkdtempSync, writeFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {createServer} from "node:http";
import {registerBenchmarkTools} from "../dist/benchmark-tools.js";

test("benchmark tools are runner-scoped and forward exact native arguments", async () => {
  const previous = process.env.CLAWTUNE_BENCHMARK_TOOLS;
  const root = mkdtempSync(join(tmpdir(), "clawtune-bridge-"));
  const calls = [];
  const server = createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    calls.push({url: req.url, auth: req.headers.authorization, body: JSON.parse(body)});
    if (JSON.parse(body).call_id === "cancel") return;
    res.end(JSON.stringify({result: 7}));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    delete process.env.CLAWTUNE_BENCHMARK_TOOLS;
    registerBenchmarkTools({registerTool() {assert.fail("daily sessions must not register task tools");}});
    const path = join(root, "manifest.json");
    writeFileSync(path, JSON.stringify({schema: "clawtune.tool-bridge.v1",
      endpoint: `http://127.0.0.1:${server.address().port}/call`, token: "test-token",
      tools: [{name: "increment", parameters: {type: "object"}}]}));
    process.env.CLAWTUNE_BENCHMARK_TOOLS = path;
    let tool;
    registerBenchmarkTools({registerTool(value) {tool = value;}});
    assert.deepEqual(await tool.execute("call-1", {count: 3}), {content: [{type: "text", text: "7"}]});
    assert.deepEqual(calls[0], {url: "/call", auth: "Bearer test-token",
      body: {name: "increment", arguments: {count: 3}, call_id: "call-1"}});
    const controller = new AbortController();
    const pending = tool.execute("cancel", {}, controller.signal);
    const rejected = assert.rejects(pending, {name: "AbortError"});
    controller.abort();
    await rejected;
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    if (previous === undefined) delete process.env.CLAWTUNE_BENCHMARK_TOOLS;
    else process.env.CLAWTUNE_BENCHMARK_TOOLS = previous;
    rmSync(root, {recursive: true});
  }
});
