import {readFileSync} from "node:fs";
import {createHash} from "node:crypto";
import validate, {schemaDigest} from "./config-validator.js";
import type {PluginConfig} from "./contracts.js";

// The installed manifest is the public configuration contract for both entry
// registration and direct loadConfig callers.
export const configSchema = JSON.parse(
  readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"),
).configSchema;

if (createHash("sha256").update(JSON.stringify(configSchema)).digest("hex") !== schemaDigest) {
  throw new Error("plugin configuration schema changed; rebuild the plugin before loading it");
}
export const validateConfig = validate;

export function configDefaults(): PluginConfig {
  const result = {trace: {}};
  if (!validateConfig(result)) {
    throw new Error(`invalid plugin configuration defaults: ${JSON.stringify(validateConfig.errors)}`);
  }
  return result;
}
