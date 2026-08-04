/**
 * KB repo namespace resolution for the OpenClaw plugin.
 *
 * Every tool/model event carries a `repo` field (see CommonEvent).  The
 * scheduler sidecar keeps per-repo learning under that key in the KB "repo"
 * layer (RuntimeToolResourceKB / ClauseResourceKB).  This module resolves the
 * key for the current OpenClaw runtime so interactive use gets per-repository
 * namespaces without manual configuration, while the benchmark keeps its
 * explicit per-task key.
 *
 * Resolution priority:
 *
 *   1. `CLAW_REPO_KEY` env                 — explicit override; swe-rebench
 *                                           injects this per task, so the
 *                                           benchmark path never hits git.
 *   2. plugin config `repo`                — user value in openclaw.json
 *                                           (or OPENCLAW_AGENT_SCHEDULER_REPO).
 *   3. git remote "origin" of the process
 *      working directory                   — owner/repo, e.g. "acme/widgets".
 *   4. basename of the working directory   — non-git workspace.
 *   5. null                               — the sidecar falls back to
 *                                           AGENT_SCHEDULER_TOOL_RESOURCE_REPO.
 */

import {execFileSync} from "node:child_process";
import {basename, resolve} from "node:path";

/**
 * Resolve the KB repo namespace for this OpenClaw runtime.
 *
 * @param configured Optional plugin-config `repo` value (already loaded).
 * @param cwd        Working directory to derive from (defaults to the
 *                   process working directory).  Exposed for tests.
 */
export function resolveRepoKey(
  configured: string | null | undefined = null,
  cwd: string = process.cwd(),
): string | null {
  const fromEnv = (process.env.CLAW_REPO_KEY ?? "").trim();
  if (fromEnv) {
    return fromEnv;
  }
  const fromConfig = (configured ?? "").trim();
  if (fromConfig) {
    return fromConfig;
  }
  const fromGit = repoFromGitRemote(cwd);
  if (fromGit) {
    return fromGit;
  }
  const name = basename(resolve(cwd)).trim();
  if (name && name !== "/" && name !== "." && name !== "\\") {
    return name;
  }
  return null;
}

/**
 * Derive owner/repo from the git "origin" remote of `cwd`.
 *
 * Works from any subdirectory of a checkout.  Returns null when the
 * directory is not a git checkout, has no origin remote, or git is
 * unavailable.
 */
export function repoFromGitRemote(cwd: string): string | null {
  let firstLine = "";
  try {
    firstLine = execFileSync(
      "git",
      ["-C", cwd, "remote", "get-url", "origin"],
      {encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 5000},
    )
      .split(/\r?\n/, 1)[0]
      .trim();
  } catch {
    return null;
  }
  return repoSlugFromGitUrl(firstLine);
}

/**
 * Convert a git remote URL into an owner/repo slug.
 *
 * Understands the common remote forms:
 *   - https://host/owner/repo(.git)
 *   - ssh://git@host/owner/repo(.git)
 *   - git://host/owner/repo(.git)
 *   - git@host:owner/repo(.git)          (scp-like)
 *   - https://host/group/sub/repo(.git)  (subgroups are preserved)
 *
 * Returns null when the URL cannot be parsed into at least owner/repo.
 */
export function repoSlugFromGitUrl(url: string): string | null {
  const trimmed = (url ?? "").trim();
  if (!trimmed) {
    return null;
  }
  // Strip a trailing ".git" and any trailing slashes before parsing.
  const normalized = trimmed.replace(/\.git\/?$/, "").replace(/\/+$/, "");
  // scp-like SSH form: git@host:owner/repo
  const scp = /^[^/@]+@[^:]+:(.+)$/.exec(normalized);
  if (scp) {
    return normalizeSlug(scp[1].replace(/^\/+/, ""));
  }
  // URL forms: scheme://[user@]host/path
  const urlMatch = /^[a-z][a-z0-9+.-]*:\/\/(?:[^/@]+@)?[^/]+\/(.+)$/i.exec(
    normalized,
  );
  if (urlMatch) {
    return normalizeSlug(urlMatch[1]);
  }
  return null;
}

function normalizeSlug(path: string): string | null {
  const parts = path.split("/").filter((segment) => segment.length > 0);
  if (parts.length < 2) {
    return null;
  }
  const slug = parts.join("/");
  if (/\s/.test(slug)) {
    return null;
  }
  return slug;
}
