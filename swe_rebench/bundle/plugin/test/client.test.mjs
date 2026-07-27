import test from "node:test";
import assert from "node:assert/strict";
import {SidecarClient} from "../dist/client.js";
import {loadConfig} from "../dist/config.js";

test("sidecar client sends the fixed scheduler credential when configured", async () => {
  const previousToken = process.env.OPENCLAW_SCHEDULER_TOKEN;
  const previousFetch = globalThis.fetch;
  let requestHeaders;
  process.env.OPENCLAW_SCHEDULER_TOKEN = "scheduler-test-token";
  globalThis.fetch = async (_url, init) => {
    requestHeaders = init.headers;
    return new Response("{}", {
      status: 200,
      headers: {"content-type": "application/json"},
    });
  };

  try {
    const client = new SidecarClient(loadConfig({}));
    await client.reportModel({});

    assert.equal(requestHeaders.authorization, "Bearer scheduler-test-token");
    assert.equal(requestHeaders["content-type"], "application/json");
  } finally {
    globalThis.fetch = previousFetch;
    if (previousToken === undefined) {
      delete process.env.OPENCLAW_SCHEDULER_TOKEN;
    } else {
      process.env.OPENCLAW_SCHEDULER_TOKEN = previousToken;
    }
  }
});

test("sidecar client omits authorization when no scheduler credential exists", async () => {
  const previousToken = process.env.OPENCLAW_SCHEDULER_TOKEN;
  const previousFetch = globalThis.fetch;
  let requestHeaders;
  delete process.env.OPENCLAW_SCHEDULER_TOKEN;
  globalThis.fetch = async (_url, init) => {
    requestHeaders = init.headers;
    return new Response("{}", {
      status: 200,
      headers: {"content-type": "application/json"},
    });
  };

  try {
    const client = new SidecarClient(loadConfig({}));
    await client.reportModel({});

    assert.equal(requestHeaders.authorization, undefined);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousToken === undefined) {
      delete process.env.OPENCLAW_SCHEDULER_TOKEN;
    } else {
      process.env.OPENCLAW_SCHEDULER_TOKEN = previousToken;
    }
  }
});
