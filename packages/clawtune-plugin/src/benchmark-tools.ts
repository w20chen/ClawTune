import {readFileSync} from "node:fs";
import type {HookApi} from "openclaw/plugin-sdk/plugin-entry";

/** Runner-only task tools. Ordinary OpenClaw sessions have no bridge manifest. */
export function registerBenchmarkTools(api: HookApi): void {
  const path = process.env.CLAWTUNE_BENCHMARK_TOOLS;
  if (!path) return;
  const bridge = JSON.parse(readFileSync(path, "utf8"));
  const url = new URL(bridge.endpoint);
  if (bridge.schema !== "clawtune.tool-bridge.v1" || url.hostname !== "127.0.0.1" || url.protocol !== "http:" || !Array.isArray(bridge.tools)) {
    throw new Error("Invalid benchmark tool bridge");
  }
  if (!api.registerTool) throw new Error("OpenClaw runtime does not expose registerTool");
  for (const tool of bridge.tools) {
    api.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description || tool.name,
      parameters: tool.parameters,
      async execute(id: string, params: Record<string, unknown>) {
        const response = await fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json", Authorization: `Bearer ${bridge.token}`},
          body: JSON.stringify({name: tool.name, arguments: params, call_id: id}),
          signal: AbortSignal.timeout(300_000),
        });
        if (!response.ok) throw new Error(`Benchmark tool failed: ${response.status} ${await response.text()}`);
        const data = await response.json() as {result: unknown};
        return {content: [{type: "text", text: typeof data.result === "string" ? data.result : JSON.stringify(data.result)}]};
      },
    });
  }
}
