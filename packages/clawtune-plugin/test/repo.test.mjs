import test from "node:test";
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {mkdtempSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";

import {
  repoSlugFromGitUrl,
  repoFromGitRemote,
  resolveRepoKey,
} from "../dist/repo.js";

const ENV_KEY = "CLAWTUNE_REPO_KEY";

function withRepoEnv(value, fn) {
  const previous = process.env[ENV_KEY];
  if (value === null) {
    delete process.env[ENV_KEY];
  } else {
    process.env[ENV_KEY] = value;
  }
  try {
    return fn();
  } finally {
    if (previous === undefined) {
      delete process.env[ENV_KEY];
    } else {
      process.env[ENV_KEY] = previous;
    }
  }
}

test("repoSlugFromGitUrl parses the common remote forms", () => {
  assert.equal(repoSlugFromGitUrl("https://github.com/acme/widgets.git"), "acme/widgets");
  assert.equal(repoSlugFromGitUrl("https://github.com/acme/widgets"), "acme/widgets");
  assert.equal(repoSlugFromGitUrl("git@github.com:acme/widgets.git"), "acme/widgets");
  assert.equal(repoSlugFromGitUrl("ssh://git@github.com/acme/widgets.git"), "acme/widgets");
  assert.equal(repoSlugFromGitUrl("git://github.com/acme/widgets"), "acme/widgets");
  assert.equal(
    repoSlugFromGitUrl("https://gitlab.com/group/subgroup/widgets.git"),
    "group/subgroup/widgets",
  );
  assert.equal(repoSlugFromGitUrl("  https://github.com/acme/widgets.git  "), "acme/widgets");
});

test("repoSlugFromGitUrl rejects unparsable values", () => {
  assert.equal(repoSlugFromGitUrl(""), null);
  assert.equal(repoSlugFromGitUrl("   "), null);
  assert.equal(repoSlugFromGitUrl("not-a-url"), null);
  assert.equal(repoSlugFromGitUrl("https://github.com/single"), null);
  assert.equal(repoSlugFromGitUrl("acme widgets"), null);
});

test("resolveRepoKey honours CLAWTUNE_REPO_KEY over config, git, and basename", () => {
  withRepoEnv("acme/explicit", () => {
    assert.equal(resolveRepoKey("config/repo", tmpdir()), "acme/explicit");
    assert.equal(resolveRepoKey(null, tmpdir()), "acme/explicit");
  });
});

test("resolveRepoKey honours plugin config repo over git and basename", () => {
  withRepoEnv(null, () => {
    assert.equal(resolveRepoKey("config/repo", tmpdir()), "config/repo");
    assert.equal(resolveRepoKey("  config/repo  ", tmpdir()), "config/repo");
    // Empty config falls through to git/basename derivation.
    assert.equal(resolveRepoKey("", tmpdir()), tmpdir().split(/[\\/]/).pop());
  });
});

test("resolveRepoKey falls back to the working-directory basename", () => {
  withRepoEnv(null, () => {
    const dir = mkdtempSync(join(tmpdir(), "clawtune-repo-test-"));
    try {
      // The OS temp root is not a git checkout, so the fallback is basename.
      assert.equal(resolveRepoKey(null, dir), dir.split(/[\\/]/).pop());
    } finally {
      rmSync(dir, {recursive: true, force: true});
    }
  });
});

test("repoFromGitRemote reads origin from a real checkout", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "clawtune-git-test-"));
  try {
    try {
      execFileSync("git", ["-C", dir, "init", "-q"]);
      execFileSync(
        "git",
        ["-C", dir, "remote", "add", "origin", "git@github.com:acme/widgets.git"],
      );
    } catch (error) {
      t.skip(`git unavailable in test environment: ${error}`);
      return;
    }
    assert.equal(repoFromGitRemote(dir), "acme/widgets");
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
});

test("repoFromGitRemote returns null for a non-checkout directory", () => {
  const dir = mkdtempSync(join(tmpdir(), "clawtune-notgit-"));
  try {
    assert.equal(repoFromGitRemote(dir), null);
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
});
