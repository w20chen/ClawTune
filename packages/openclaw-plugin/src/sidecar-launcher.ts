/**
 * Sidecar process launcher.
 *
 * When autoStartSidecar is enabled, this module checks whether the scheduler
 * sidecar is already reachable at the configured endpoint and, if not, spawns
 * it as a child process.  It returns a cleanup handle so the plugin can
 * terminate the sidecar on shutdown.
 */

import {spawn, type ChildProcess} from "node:child_process";
import {existsSync, realpathSync, statSync} from "node:fs";
import {release as kernelRelease} from "node:os";
import {isAbsolute, join} from "node:path";
import {fileURLToPath} from "node:url";

const PRIVILEGED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
const PRESERVED_RUNTIME_ENV = new Set([
  "DOCKER_CONFIG",
  "DOCKER_CONTEXT",
  "DOCKER_HOST",
  "DOCKER_CERT_PATH",
  "DOCKER_TLS_VERIFY",
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NO_PROXY",
  "ALL_PROXY",
  "http_proxy",
  "https_proxy",
  "no_proxy",
  "all_proxy",
  "LANG",
  "LC_ALL",
  "SSL_CERT_FILE",
  "SSL_CERT_DIR",
  "REQUESTS_CA_BUNDLE",
  "CURL_CA_BUNDLE",
  "NODE_EXTRA_CA_CERTS",
  "XDG_RUNTIME_DIR",
]);

// ── Public API ──────────────────────────────────────────────────────────

export interface SidecarLauncherResult {
  /** The spawned child process, or null if the sidecar was already running. */
  child: ChildProcess | null;
  /** Call this to stop the auto-started sidecar (no-op for pre-existing). */
  cleanup: () => void;
}

export interface SidecarLaunchOptions {
  endpoint: string;
  command: string;
  /** Time to wait between health-check attempts (ms). */
  healthPollMs: number;
  /** Maximum time to wait for the sidecar to become healthy (ms). */
  healthTimeoutMs: number;
  /** Logger-compatible object. */
  logger: {
    info(message: string, data?: unknown): void;
    warn(message: string, data?: unknown): void;
    error(message: string, data?: unknown): void;
  };
}

export interface SidecarRuntime {
  platform: string;
  kernelRelease: string;
  env: NodeJS.ProcessEnv;
}

export interface SidecarProcessSpec {
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
}

/**
 * OpenClaw can discover the same plugin through more than one load path. A
 * module-local promise is not enough in that case because Node may evaluate
 * two copies of this module. Keep only in-flight launches on globalThis,
 * keyed by health URL, and use Symbol.for so those copies share a registry.
 */
const sharedLaunchesSymbol = Symbol.for(
  "clawtune.openclaw-plugin.sidecar-launches.v1",
);

type SharedLaunchRegistry = Map<string, Promise<SidecarLauncherResult>>;

function sharedLaunches(): SharedLaunchRegistry {
  const sharedGlobal = globalThis as unknown as {[key: symbol]: unknown};
  const existing = sharedGlobal[sharedLaunchesSymbol];
  if (existing instanceof Map) {
    return existing as SharedLaunchRegistry;
  }

  const registry: SharedLaunchRegistry = new Map();
  sharedGlobal[sharedLaunchesSymbol] = registry;
  return registry;
}

/**
 * Build a default command & environment for auto-starting the sidecar.
 *
 * Returns null when the project layout cannot be detected.  The caller
 * should fall back to the user-provided command or a plain `python -m
 * agent_scheduler.main` invocation with PYTHONPATH.
 */
function defaultSidecarEnv(endpoint: string): {
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
} | null {
  const projectRoot = resolveProjectRoot();
  if (!projectRoot) return null;

  const url = new URL(endpoint);
  const host = listenHost(url);
  const port = url.port || "8765";

  // A normal ClawTune checkout installs the scheduler into .venv with access
  // to the distribution BCC bindings. Resolve every path from the loaded
  // plugin at launch time so moving the checkout does not leave a stale
  // absolute sidecar command in openclaw.json. eBPF attachment still needs
  // root; invoke sudo directly (without a shell) and keep a conservative PATH
  // for the privileged compiler/toolchain lookup.
  const privileged = buildPrivilegedSidecarLaunch(projectRoot, endpoint);
  if (privileged) return privileged;

  const python = pythonCommand();
  const schedulerSrc = join(projectRoot, "services", "scheduler", "src");

  const sep = process.platform === "win32" ? ";" : ":";
  const existingPythonpath = process.env.PYTHONPATH;
  const pythonpath = existingPythonpath
    ? schedulerSrc + sep + existingPythonpath
    : schedulerSrc;

  return {
    command: python,
    args: ["-m", "agent_scheduler.main", "--host", host, "--port", port],
    env: {...process.env, PYTHONPATH: pythonpath},
  };
}

/**
 * Ensure the sidecar is running.  Spawns it when auto-start is enabled and
 * the sidecar is not already reachable.
 */
export function ensureSidecarRunning(
  opts: SidecarLaunchOptions,
): Promise<SidecarLauncherResult> {
  const healthUrl = sidecarHealthUrl(opts.endpoint);
  const registry = sharedLaunches();
  const existing = registry.get(healthUrl);
  if (existing) {
    opts.logger.info("joining in-flight sidecar auto-start", {
      endpoint: opts.endpoint,
    });
    return existing;
  }

  const launch = ensureSidecarRunningOnce(opts).then(makeCleanupIdempotent);
  registry.set(healthUrl, launch);

  // This is a single-flight coordinator, not a permanent result cache. A
  // later registration performs a fresh health check. Concurrent callers get
  // the exact same result and therefore the same idempotent cleanup handle.
  void launch.then(
    () => {
      if (registry.get(healthUrl) === launch) registry.delete(healthUrl);
    },
    () => {
      if (registry.get(healthUrl) === launch) registry.delete(healthUrl);
    },
  );

  return launch;
}

export function buildPrivilegedSidecarLaunch(
  projectRoot: string,
  endpoint: string,
  runtime: SidecarRuntime = {
    platform: process.platform,
    kernelRelease: kernelRelease(),
    env: process.env,
  },
): SidecarProcessSpec | null {
  if (runtime.platform !== "linux") return null;
  const venvPython = join(projectRoot, ".venv", "bin", "python");
  if (!existsSync(venvPython)) return null;

  const url = new URL(endpoint);
  const configuredKernelSource = runtime.env.BCC_KERNEL_SOURCE;
  const kernelCandidate = configuredKernelSource && existsSync(configuredKernelSource)
    ? configuredKernelSource
    : join("/lib/modules", runtime.kernelRelease, "build");
  if (!existsSync(kernelCandidate)) {
    throw new Error(
      `kernel build tree not found at ${kernelCandidate}; run ` +
      "python3 scripts/clawtune.py setup",
    );
  }
  const kernelSource = realpathSync(kernelCandidate);
  return {
    command: "sudo",
    args: [
      ...sudoPreserveEnvironmentArgs(runtime.env),
      "env",
      `PATH=${privilegedPath(runtime.env, runtime.platform)}`,
      `HOME=${runtime.env.HOME ?? "/root"}`,
      "PYTHONNOUSERSITE=1",
      // This branch is Linux-only. Do not use the host Node process's path
      // delimiter: cross-platform tests can construct a Linux runtime.
      `PYTHONPATH=${projectRoot}:${join(projectRoot, "services", "scheduler", "src")}`,
      `BCC_KERNEL_SOURCE=${kernelSource}`,
      `AGENT_SCHEDULER_ENV_FILE=${join(projectRoot, ".env")}`,
      venvPython,
      "-m",
      "agent_scheduler.main",
      "--host",
      listenHost(url),
      "--port",
      url.port || "8765",
    ],
    env: {...runtime.env},
  };
}

async function ensureSidecarRunningOnce(
  opts: SidecarLaunchOptions,
): Promise<SidecarLauncherResult> {
  const {endpoint, command, healthPollMs, healthTimeoutMs, logger} = opts;
  const url = new URL(endpoint);
  const healthUrl = sidecarHealthUrl(endpoint);

  // 1. Quick check: is the sidecar already running?
  const alreadyRunning = await pingHealth(healthUrl, 800);
  if (alreadyRunning) {
    logger.info("sidecar already running, skipping auto-start", {endpoint});
    return {child: null, cleanup: () => {}};
  }

  // 2. Spawn the sidecar
  let child: ChildProcess;
  let resolvedCommand: string;

  if (command) {
    // User-provided shell command
    resolvedCommand = command;
    logger.info("auto-starting sidecar with custom command", {command: resolvedCommand});
    child = spawn(resolvedCommand, {
      shell: true,
      stdio: ["ignore", "pipe", "pipe"],
      detached: false,
      env: {...process.env},
    });
  } else {
    // Default: use -m agent_scheduler.main with PYTHONPATH
    const defaults = defaultSidecarEnv(endpoint);
    if (defaults) {
      resolvedCommand = `${defaults.command} ${defaults.args.join(" ")}`;
      logger.info("auto-starting sidecar (detected project layout)", {
        command: resolvedCommand,
        pythonpath: defaults.env.PYTHONPATH,
      });
      child = spawn(defaults.command, defaults.args, {
        stdio: ["ignore", "pipe", "pipe"],
        detached: false,
        env: defaults.env,
      });
    } else {
      // Last resort: plain python -m with PYTHONPATH from env
      const python = pythonCommand();
      const host = listenHost(url);
      const port = url.port || "8765";
      resolvedCommand = `${python} -m agent_scheduler.main --host ${host} --port ${port}`;
      logger.info("auto-starting sidecar (fallback)", {command: resolvedCommand});
      child = spawn(python, ["-m", "agent_scheduler.main", "--host", host, "--port", port], {
        stdio: ["ignore", "pipe", "pipe"],
        detached: false,
        env: {...process.env},
      });
    }
  }

  let stderr = "";
  let spawnErrorMessage: string | null = null;
  let healthEstablished = false;
  let cleanupRequested = false;
  child.stderr?.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
    // Prevent unbounded memory growth for long-running sidecars
    if (stderr.length > 8192) {
      stderr = stderr.slice(-4096);
    }
  });
  // Uvicorn and dependencies can write access/startup output to stdout.
  // Always consume that pipe so a long-lived sidecar cannot block after the
  // OS pipe buffer fills.
  child.stdout?.resume();

  child.on("exit", (code, signal) => {
    if (healthEstablished && !cleanupRequested) {
      logger.warn("sidecar exited unexpectedly", {code, signal, stderr: stderr.slice(-500)});
    }
  });

  child.on("error", (err) => {
    spawnErrorMessage = err.message;
    logger.error("sidecar process error", {error: err.message});
  });

  // 3. Wait for the sidecar to become healthy
  const deadline = Date.now() + healthTimeoutMs;

  while (Date.now() < deadline) {
    const healthy = await pingHealth(healthUrl, Math.min(healthPollMs, deadline - Date.now()));
    if (healthy) {
      // Another OpenClaw process can win the same endpoint race after both
      // processes observed it as unhealthy. A failed or daemonizing launch
      // command is not an error when the endpoint is now healthy.
      if (hasExited(child)) {
        logger.info("sidecar is healthy after another launcher won the startup race", {
          endpoint,
          exitCode: child.exitCode,
          signal: child.signalCode,
        });
        return {child: null, cleanup: () => {}};
      }

      healthEstablished = true;
      logger.info("sidecar is healthy", {endpoint});
      unrefHealthyChild(child);
      return {
        child,
        cleanup: () => {
          if (cleanupRequested) return;
          cleanupRequested = true;
          logger.info("stopping auto-started sidecar");
          if (process.platform === "win32") {
            child.kill();
          } else {
            child.kill("SIGTERM");
          }
          // Force kill after grace period.
          // Windows does not support POSIX signals; use taskkill /F as a
          // last resort when the initial kill did not terminate the child.
          setTimeout(() => {
            if (!hasExited(child)) {
              if (process.platform === "win32") {
                // On Windows, child.kill() without a signal already
                // maps to TerminateProcess.  If the child survived
                // that we try taskkill /F /PID as a forceful fallback.
                spawn("taskkill", ["/F", "/PID", String(child.pid)], {
                  stdio: "ignore",
                });
              } else {
                child.kill("SIGKILL");
              }
            }
          }, 3000).unref();
        },
      };
    }

    // Wait before next poll
    await sleep(healthPollMs);
  }

  // Do not fail immediately when the launch process exits. A concurrently
  // launched Python sidecar may take most of the configured startup window to
  // import its dependencies, particularly on emulated or low-power hosts.
  if (hasExited(child)) {
    const cause = spawnErrorMessage !== null
      ? `process error: ${spawnErrorMessage}`
      : `exit code ${String(child.exitCode)}, signal ${String(child.signalCode)}`;
    throw new Error(
      `sidecar launch ended (${cause}) before the endpoint became healthy. ` +
      `stderr: ${stderr.slice(-500)}`
    );
  }

  cleanupRequested = true;
  child.kill();
  throw new Error(
    `sidecar did not become healthy within ${healthTimeoutMs}ms. ` +
    `stderr: ${stderr.slice(-500)}`
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────

function sidecarHealthUrl(endpoint: string): string {
  return `${new URL(endpoint).origin}/health/live`;
}

function listenHost(url: URL): string {
  // URL.hostname preserves brackets for IPv6 literals in Node. Uvicorn wants
  // the bare address when it receives --host as a separate argv item.
  return url.hostname.replace(/^\[(.*)\]$/, "$1");
}

function hasExited(child: ChildProcess): boolean {
  return child.exitCode !== null || child.signalCode !== null;
}

function makeCleanupIdempotent(
  result: SidecarLauncherResult,
): SidecarLauncherResult {
  let cleanedUp = false;
  return {
    child: result.child,
    cleanup: () => {
      if (cleanedUp) return;
      cleanedUp = true;
      result.cleanup();
    },
  };
}

async function pingHealth(url: string, timeoutMs: number): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(1, timeoutMs));
  try {
    const response = await fetch(url, {
      method: "GET",
      signal: controller.signal,
    });
    if (!response.ok) return false;
    const payload: unknown = await response.json();
    return isHealthPayload(payload);
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isHealthPayload(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const payload = value as Record<string, unknown>;
  return payload.service === "clawtune-scheduler"
    && payload.schema_version === "scheduler.health.v1"
    && payload.live === true;
}

function sudoPreserveEnvironmentArgs(env: NodeJS.ProcessEnv): string[] {
  const names = Object.keys(env).filter((name) => {
    return PRESERVED_RUNTIME_ENV.has(name)
      || name.startsWith("LC_")
      || name.startsWith("AGENT_SCHEDULER_")
      || name.startsWith("CLAWTUNE_");
  });
  return names.length === 0 ? [] : [`--preserve-env=${names.sort().join(",")}`];
}

function privilegedPath(env: NodeJS.ProcessEnv, runtimePlatform: string): string {
  const pathDelimiter = runtimePlatform === "win32" ? ";" : ":";
  const inherited = (env.PATH ?? "")
    .split(pathDelimiter)
    .filter((entry) => entry.length > 0 && isAbsolute(entry));
  return [...new Set([...PRIVILEGED_PATH.split(":"), ...inherited])].join(":");
}

function unrefHealthyChild(child: ChildProcess): void {
  // The plugin retains the ChildProcess object for shutdown cleanup, but the
  // healthy sidecar and its capture pipes must not keep one-shot OpenClaw CLI
  // commands alive after the agent turn has completed.
  child.unref();
  for (const stream of [child.stdout, child.stderr]) {
    const candidate = stream as unknown as {unref?: () => void} | null;
    candidate?.unref?.();
  }
}

function pythonCommand(): string {
  if (process.platform === "win32") return "python";
  return process.env.VIRTUAL_ENV ? "python" : "python3";
}

function resolveProjectRoot(): string | null {
  // Check SIDECAR_PROJECT_ROOT env var
  const envRoot = process.env.OPENCLAW_AGENT_SCHEDULER_PROJECT_ROOT
    || process.env.SIDECAR_PROJECT_ROOT;
  if (envRoot) return envRoot;

  // Check relative to the plugin's own location
  try {
    // __dirname equivalent for ESM
    const moduleDir = fileURLToPath(new URL(".", import.meta.url));
    // Walk up: from dist/ -> packages/openclaw-plugin/ -> project root
    let dir = moduleDir;
    for (let i = 0; i < 5; i++) {
      // Look for services/scheduler/src/agent_scheduler/main.py
      const candidate = join(dir, "services", "scheduler", "src", "agent_scheduler", "main.py");
      try {
        statSync(candidate);
        return dir;
      } catch {
        // Not found at this level, go up
      }
      const parent = join(dir, "..");
      if (parent === dir) break; // Reached filesystem root
      dir = parent;
    }
  } catch {
    // Ignore resolution failures
  }

  return null;
}
