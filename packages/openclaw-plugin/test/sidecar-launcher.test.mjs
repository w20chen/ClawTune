import assert from "node:assert/strict";
import {mkdir, mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import {createServer} from "node:http";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {fileURLToPath} from "node:url";
import test from "node:test";

import {
  buildPrivilegedSidecarLaunch,
  ensureSidecarRunning,
} from "../dist/sidecar-launcher.js";

function shellArgument(value) {
  return `"${value.replaceAll('"', '\\"')}"`;
}

test("privileged sidecar launch resolves the checkout and preserves a narrow environment", async () => {
  const projectRoot = await mkdtemp(join(tmpdir(), "clawtune-sidecar-project-"));
  const kernelSource = join(projectRoot, "kernel-build");
  const venvPython = join(projectRoot, ".venv", "bin", "python");
  const customBin = "/opt/clawtune-custom-bin";
  await mkdir(join(projectRoot, ".venv", "bin"), {recursive: true});
  await mkdir(kernelSource);
  await writeFile(venvPython, "");

  try {
    const proxyValue = "http://credential-must-not-enter-argv@example.invalid";
    const spec = buildPrivilegedSidecarLaunch(
      projectRoot,
      "http://127.0.0.1:9123",
      {
        platform: "linux",
        kernelRelease: "test-kernel",
        env: {
          PATH: `relative-bin:${customBin}`,
          BCC_KERNEL_SOURCE: kernelSource,
          HTTPS_PROXY: proxyValue,
          AGENT_SCHEDULER_POLICY: "observe-only",
          UNRELATED_VALUE: "not-preserved-by-sudo",
        },
      },
    );

    assert(spec);
    assert.equal(spec.command, "sudo");
    assert(spec.args.includes(venvPython));
    assert(spec.args.includes(`PYTHONPATH=${projectRoot}`));
    assert(spec.args.includes(`BCC_KERNEL_SOURCE=${kernelSource}`));
    assert(spec.args.includes("--host"));
    assert(spec.args.includes("127.0.0.1"));
    assert(spec.args.includes("9123"));
    const preserve = spec.args.find((value) => value.startsWith("--preserve-env="));
    assert(preserve?.includes("HTTPS_PROXY"));
    assert(preserve?.includes("AGENT_SCHEDULER_POLICY"));
    assert(!preserve?.includes("UNRELATED_VALUE"));
    assert(!spec.args.some((value) => value.includes(proxyValue)));
    const pathArg = spec.args.find((value) => value.startsWith("PATH="));
    assert(pathArg?.includes(customBin));
    assert(!pathArg?.includes("relative-bin"));
  } finally {
    await rm(projectRoot, {recursive: true, force: true});
  }
});

test("sidecar auto-start is single-flight and tolerates another launcher winning", async () => {
  let healthy = false;
  const server = createServer((request, response) => {
    if (request.url === "/health/live" && healthy) {
      response.writeHead(200, {"content-type": "application/json"}).end(JSON.stringify({
        schema_version: "scheduler.health.v1",
        service: "clawtune-scheduler",
        live: true,
      }));
    } else {
      response.writeHead(503).end("starting");
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });

  const address = server.address();
  assert(address && typeof address === "object");
  const endpoint = `http://127.0.0.1:${address.port}`;
  const workDir = await mkdtemp(join(tmpdir(), "clawtune-sidecar-launch-"));
  const attemptsPath = join(workDir, "attempts.txt");
  const fixturePath = fileURLToPath(
    new URL("./fixtures/sidecar-launch-attempt.mjs", import.meta.url),
  );
  const command = [process.execPath, fixturePath, attemptsPath]
    .map(shellArgument)
    .join(" ");
  const messages = [];
  const logger = {
    info: (message) => messages.push(["info", message]),
    warn: (message) => messages.push(["warn", message]),
    error: (message) => messages.push(["error", message]),
  };

  const healthyTimer = setTimeout(() => {
    healthy = true;
  }, 200);

  try {
    const opts = {
      endpoint,
      command,
      healthPollMs: 20,
      healthTimeoutMs: 2_000,
      logger,
    };
    const [first, second] = await Promise.all([
      ensureSidecarRunning(opts),
      ensureSidecarRunning(opts),
    ]);

    assert.strictEqual(first, second);
    assert.equal(first.child, null);
    assert.equal(await readFile(attemptsPath, "utf8"), "attempt\n");
    assert.equal(
      messages.filter(([, message]) => message === "joining in-flight sidecar auto-start").length,
      1,
    );
    assert.equal(
      messages.filter(([, message]) => message.includes("won the startup race")).length,
      1,
    );

    // Both plugin instances receive the same safe, repeatable cleanup handle.
    first.cleanup();
    second.cleanup();
  } finally {
    clearTimeout(healthyTimer);
    await new Promise((resolve) => server.close(resolve));
    await rm(workDir, {recursive: true, force: true});
  }
});
