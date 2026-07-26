export type SandboxPathEnv = {
  hostWorkspace?: string;
  containerWorkspace?: string;
  execWorkdir?: string;
};

export function normalizeSandboxToolParams(
  params: Record<string, unknown> | null,
  toolName: string,
  env: SandboxPathEnv = {
    hostWorkspace: process.env.CLAW_SANDBOX_HOST_WORKSPACE,
    containerWorkspace: process.env.CLAW_SANDBOX_CONTAINER_WORKSPACE,
    execWorkdir: process.env.CLAW_EXEC_WORKDIR,
  }
): {params: Record<string, unknown> | null; changed: boolean} {
  if (params === null) return {params: null, changed: false};
  const hostWorkspace = normalizePathEnv(env.hostWorkspace);
  const containerWorkspace = normalizePathEnv(env.containerWorkspace)
    ?? normalizePathEnv(env.execWorkdir)
    ?? "/workspace";
  if (hostWorkspace === null) return {params, changed: false};
  const targetWorkspace = usesContainerWorkspace(toolName) ? containerWorkspace : null;
  const containerAliases = containerWorkspaceAliases(hostWorkspace, containerWorkspace);

  let changed = false;
  const normalized = rewritePathFields(
    params,
    hostWorkspace,
    containerAliases,
    targetWorkspace,
    (didChange) => {
      changed = changed || didChange;
    }
  );

  if (toolName === "exec") {
    if (normalized.host === "gateway") {
      delete normalized.host;
      changed = true;
    }
    if (normalized.elevated === true) {
      delete normalized.elevated;
      changed = true;
    }
    changed = normalizeExecWorkdirField(normalized, "cwd", hostWorkspace, containerAliases, containerWorkspace) || changed;
    changed = normalizeExecWorkdirField(normalized, "workdir", hostWorkspace, containerAliases, containerWorkspace) || changed;
  }

  return {params: normalized, changed};
}

function normalizeExecWorkdirField(
  params: Record<string, unknown>,
  key: "cwd" | "workdir",
  hostWorkspace: string,
  containerAliases: string[],
  containerWorkspace: string
): boolean {
  if (typeof params[key] === "string") {
    const mapped = mapWorkspacePath(params[key], hostWorkspace, containerAliases, containerWorkspace);
    if (mapped !== params[key]) {
      params[key] = mapped;
      return true;
    }
    return false;
  }
  params[key] = containerWorkspace;
  return true;
}

function rewritePathFields(
  value: Record<string, unknown>,
  hostWorkspace: string,
  containerAliases: string[],
  targetWorkspace: string | null,
  markChanged: (changed: boolean) => void
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "string" && isPathLikeKey(key)) {
      const mapped = mapWorkspacePath(item, hostWorkspace, containerAliases, targetWorkspace);
      out[key] = mapped;
      markChanged(mapped !== item);
      continue;
    }
    if (isPlainRecord(item)) {
      out[key] = rewritePathFields(
        item as Record<string, unknown>,
        hostWorkspace,
        containerAliases,
        targetWorkspace,
        markChanged
      );
      continue;
    }
    if (Array.isArray(item)) {
      out[key] = item.map((entry) => {
        if (isPlainRecord(entry)) {
          return rewritePathFields(
            entry as Record<string, unknown>,
            hostWorkspace,
            containerAliases,
            targetWorkspace,
            markChanged
          );
        }
        return entry;
      });
      continue;
    }
    out[key] = item;
  }
  return out;
}

function isPathLikeKey(key: string): boolean {
  const normalized = key.toLowerCase();
  return [
    "path",
    "file",
    "filename",
    "filepath",
    "target",
    "source",
    "destination",
    "dest",
    "cwd",
    "workdir",
    "workingdirectory",
  ].includes(normalized);
}

function mapWorkspacePath(
  value: string,
  hostWorkspace: string,
  containerAliases: string[],
  targetWorkspace: string | null
): string {
  const normalized = normalizePathString(value);
  if (normalized === hostWorkspace) return targetWorkspace ?? ".";
  if (normalized.startsWith(`${hostWorkspace}/`)) {
    const suffix = normalized.slice(hostWorkspace.length + 1);
    return targetWorkspace === null ? suffix : `${targetWorkspace}/${suffix}`;
  }
  for (const alias of containerAliases) {
    if (normalized === alias) return targetWorkspace ?? ".";
    if (normalized.startsWith(`${alias}/`)) {
      const suffix = normalized.slice(alias.length + 1);
      return targetWorkspace === null ? suffix : `${targetWorkspace}/${suffix}`;
    }
  }
  return value;
}

function containerWorkspaceAliases(hostWorkspace: string, containerWorkspace: string): string[] {
  const aliases = [containerWorkspace];
  const hostBasename = hostWorkspace.split("/").filter(Boolean).at(-1);
  if (hostBasename !== undefined) aliases.push(`${containerWorkspace}/${hostBasename}`);
  return [...new Set(aliases)].sort((left, right) => right.length - left.length);
}

function usesContainerWorkspace(toolName: string): boolean {
  return toolName === "exec" || toolName === "process";
}

function normalizePathEnv(value: string | undefined): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  return normalizePathString(value).replace(/\/+$/g, "") || "/";
}

function normalizePathString(value: string): string {
  return value.replace(/\\/g, "/").replace(/\/+/g, "/").replace(/\/+$/g, "") || "/";
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
