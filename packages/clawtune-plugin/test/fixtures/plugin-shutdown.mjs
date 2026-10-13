import {register} from "node:module";
import {fileURLToPath} from "node:url";

// Exercise the real plugin hooks without installing or modifying OpenClaw.
register(`data:text/javascript,${encodeURIComponent(`
  export async function resolve(specifier, context, nextResolve) {
    if (specifier === 'openclaw/plugin-sdk/plugin-entry') {
      return {url: 'data:text/javascript,export const definePluginEntry = entry => entry', shortCircuit: true};
    }
    return nextResolve(specifier, context);
  }
`)}`, import.meta.url);
const {default: plugin} = await import("../../dist/index.js");
const [mode, port, directory] = process.argv.slice(2);
const fixture = fileURLToPath(new URL("./sidecar-slow-shutdown.mjs", import.meta.url));
const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
const command = [process.execPath, fixture, port, directory].map(quote).join(" ");
const api = {
  pluginConfig: {
    endpoint: `http://127.0.0.1:${port}`, autoStartSidecar: true,
    sidecarCommand: (mode === "beforeExit" ? "exec " : "") + command,
    sidecarStartupTimeoutMs: 5_000,
  },
  on() {},
  logger: {
    info(message) {
      if (message === "sidecar is healthy") {
        process.send("ready", () => {
          if (mode === "beforeExit") process.disconnect();
        });
      }
    },
    warn: console.error,
    error: console.error,
  },
};
plugin.register(api);
if (mode === "shared") plugin.register(api);
