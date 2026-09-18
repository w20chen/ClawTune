import {readFileSync} from "node:fs";
import {request} from "node:http";
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
      async execute(id: string, params: Record<string, unknown>, signal?: AbortSignal) {
        // node:http has no implicit fetch/Undici headers deadline. The runner
        // owns the task deadline; preserve OpenClaw's per-call cancellation.
        const body = JSON.stringify({name: tool.name, arguments: params, call_id: id});
        const data = await new Promise<{result: unknown}>((resolve, reject) => {
          const req = request(url, {
            method: "POST",
            headers: {"Content-Type": "application/json", Authorization: `Bearer ${bridge.token}`,
              "Content-Length": Buffer.byteLength(body)},
            signal,
          }, (response) => {
            const chunks: Buffer[] = [];
            response.on("data", (chunk: Buffer) => chunks.push(chunk));
            response.on("error", reject);
            response.on("end", () => {
              const text = Buffer.concat(chunks).toString("utf8");
              if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
                reject(new Error(`Benchmark tool failed: ${response.statusCode} ${text}`));
                return;
              }
              try { resolve(JSON.parse(text)); } catch (error) { reject(error); }
            });
          });
          req.on("error", reject);
          req.end(body);
        });
        return {content: [{type: "text", text: typeof data.result === "string" ? data.result : JSON.stringify(data.result)}]};
      },
    });
  }
}
