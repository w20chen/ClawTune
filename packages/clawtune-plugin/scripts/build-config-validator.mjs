import {readFileSync, mkdirSync, writeFileSync} from "node:fs";
import {createHash} from "node:crypto";
import {Ajv} from "ajv";
import standaloneCode from "ajv/dist/standalone/index.js";

const schema = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8")).configSchema;
const ajv = new Ajv({allErrors: true, useDefaults: true, strict: false, code: {source: true, esm: true}});
const source = standaloneCode(ajv, ajv.compile(schema));
// Staged plugins intentionally have no node_modules. Do not accidentally
// introduce a runtime dependency when the public schema evolves.
if (/\brequire\s*\(|\bimport\s/.test(source)) {
  throw new Error("configuration validator must be self-contained for staged plugins");
}
const digest = createHash("sha256").update(JSON.stringify(schema)).digest("hex");
mkdirSync(new URL("../dist/", import.meta.url), {recursive: true});
writeFileSync(new URL("../dist/config-validator.js", import.meta.url),
  source + `\nexport const schemaDigest = ${JSON.stringify(digest)};\n`);
writeFileSync(new URL("../dist/config-validator.d.ts", import.meta.url),
  readFileSync(new URL("../src/config-validator.d.ts", import.meta.url)));
