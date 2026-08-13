import test from "node:test";
import assert from "node:assert/strict";
import {SidecarClient} from "../dist/client.js";
import {loadConfig} from "../dist/config.js";

test("sidecar client sends the fixed ClawTune credential when configured", async () => {
  const previousToken = process.env.CLAWTUNE_TOKEN;
  const previousFetch = globalThis.fetch;
  let requestHeaders;
  process.env.CLAWTUNE_TOKEN = "scheduler-test-token";
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
      delete process.env.CLAWTUNE_TOKEN;
    } else {
      process.env.CLAWTUNE_TOKEN = previousToken;
    }
  }
});

test("sidecar client omits authorization when no ClawTune credential exists", async () => {
  const previousToken = process.env.CLAWTUNE_TOKEN;
  const previousFetch = globalThis.fetch;
  let requestHeaders;
  delete process.env.CLAWTUNE_TOKEN;
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
      delete process.env.CLAWTUNE_TOKEN;
    } else {
      process.env.CLAWTUNE_TOKEN = previousToken;
    }
  }
});

test("sidecar client fetches execution telemetry", async () => {
  const previousFetch = globalThis.fetch;
  let requestUrl;
  globalThis.fetch = async (url) => {
    requestUrl = String(url);
    return new Response(JSON.stringify({
      tool_resource: {
        execution_id: "exec-1",
        status: "completed",
      },
    }), {
      status: 200,
      headers: {"content-type": "application/json"},
    });
  };

  try {
    const client = new SidecarClient(loadConfig({endpoint: "http://scheduler.test"}));
    const telemetry = await client.getExecutionTelemetry("exec-1");

    assert.equal(requestUrl, "http://scheduler.test/v2/executions/exec-1/telemetry");
    assert.deepEqual(telemetry, {
      execution_id: "exec-1",
      status: "completed",
    });
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("sidecar client includes response body in HTTP errors", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    JSON.stringify({detail: "validation failed"}),
    {
      status: 422,
      headers: {"content-type": "application/json"},
    }
  );

  try {
    const client = new SidecarClient(loadConfig({endpoint: "http://scheduler.test"}));
    await assert.rejects(
      () => client.reportCompletion({}),
      /sidecar_http_422: .*validation failed/
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});
