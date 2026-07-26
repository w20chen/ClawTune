import test from "node:test";
import assert from "node:assert/strict";
import {normalizeSandboxToolParams} from "../dist/sandbox-paths.js";

const env = {
  hostWorkspace: "/home/weitian/claw/swe_rebench/workspaces/0b01001001__spectree-64",
  containerWorkspace: "/workspace",
};

test("sandbox path normalization maps file tool host paths to workspace-relative paths", () => {
  const result = normalizeSandboxToolParams(
    {
      path: "/home/weitian/claw/swe_rebench/workspaces/0b01001001__spectree-64/setup.py",
      cwd: "/home/weitian/claw/swe_rebench/workspaces/0b01001001__spectree-64",
    },
    "read",
    env
  );

  assert.equal(result.changed, true);
  assert.equal(result.params.path, "setup.py");
  assert.equal(result.params.cwd, ".");
});

test("sandbox path normalization leaves relative paths alone", () => {
  const result = normalizeSandboxToolParams(
    {
      path: "setup.py",
      cwd: ".",
    },
    "read",
    env
  );

  assert.equal(result.changed, false);
  assert.equal(result.params.path, "setup.py");
  assert.equal(result.params.cwd, ".");
});

test("sandbox path normalization maps container task-directory aliases for file tools", () => {
  const result = normalizeSandboxToolParams(
    {
      path: "/workspace/0b01001001__spectree-64/README.md",
      cwd: "/workspace/0b01001001__spectree-64",
    },
    "read",
    env
  );

  assert.equal(result.changed, true);
  assert.equal(result.params.path, "README.md");
  assert.equal(result.params.cwd, ".");
});

test("sandbox path normalization strips gateway override from exec in sandbox mode", () => {
  const result = normalizeSandboxToolParams(
    {
      command: "pytest -q",
      host: "gateway",
      elevated: true,
      workdir: "/home/weitian/claw/swe_rebench/workspaces/0b01001001__spectree-64",
    },
    "exec",
    env
  );

  assert.equal(result.changed, true);
  assert.equal(result.params.command, "pytest -q");
  assert.equal(result.params.cwd, "/workspace");
  assert.equal(result.params.workdir, "/workspace");
  assert.equal("host" in result.params, false);
  assert.equal("elevated" in result.params, false);
});

test("sandbox path normalization maps container task-directory aliases for exec", () => {
  const result = normalizeSandboxToolParams(
    {
      command: "pwd",
      workdir: "/workspace/0b01001001__spectree-64",
    },
    "exec",
    env
  );

  assert.equal(result.changed, true);
  assert.equal(result.params.command, "pwd");
  assert.equal(result.params.cwd, "/workspace");
  assert.equal(result.params.workdir, "/workspace");
});

test("sandbox path normalization sets default sandbox workdir for exec", () => {
  const result = normalizeSandboxToolParams(
    {
      command: "echo hello",
    },
    "exec",
    env
  );

  assert.equal(result.changed, true);
  assert.equal(result.params.command, "echo hello");
  assert.equal(result.params.cwd, "/workspace");
  assert.equal(result.params.workdir, "/workspace");
});
