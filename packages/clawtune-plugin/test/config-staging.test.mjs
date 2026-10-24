import test from "node:test";
import assert from "node:assert/strict";
import {cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {dirname, join, resolve} from "node:path";
import {pathToFileURL} from "node:url";
import {spawnSync} from "node:child_process";

test("staged plugin configuration loads without node_modules and rejects stale schema", () => {
  const staged = mkdtempSync(join(tmpdir(), "clawtune-config-stage-"));
  try {
    cpSync(new URL("../dist", import.meta.url), join(staged, "dist"), {recursive: true});
    cpSync(new URL("../openclaw.plugin.json", import.meta.url), join(staged, "openclaw.plugin.json"));
    writeFileSync(join(staged, "package.json"), JSON.stringify({type: "module"}));
    const source = `import {loadConfig} from ${JSON.stringify(pathToFileURL(join(staged, "dist/config.js")).href)}; console.log(JSON.stringify(loadConfig({sandboxExecPredictionModel:"lattice"})));`;
    const run = () => spawnSync(process.execPath, ["--input-type=module", "-e", source], {cwd: staged, encoding: "utf8"});
    const valid = run();
    assert.equal(valid.status, 0, valid.stderr);
    assert.equal(JSON.parse(valid.stdout).sandboxExecPredictionModel, "lattice");
    const manifest = JSON.parse(readFileSync(join(staged, "openclaw.plugin.json"), "utf8"));
    manifest.configSchema.properties.mode.default = "enforce";
    writeFileSync(join(staged, "openclaw.plugin.json"), JSON.stringify(manifest));
    const stale = run();
    assert.notEqual(stale.status, 0);
    assert.match(stale.stderr, /configuration schema changed; rebuild/);
  } finally {
    assert.equal(dirname(resolve(staged)), resolve(tmpdir()));
    rmSync(staged, {recursive: true, force: true});
  }
});
