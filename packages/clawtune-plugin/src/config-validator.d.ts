import type {PluginConfig} from "./contracts.js";

export interface ConfigValidator {
  (data: unknown): data is PluginConfig;
  errors?: null | Array<{instancePath: string; message?: string}>;
}
declare const validate: ConfigValidator;
export const schemaDigest: string;
export default validate;
