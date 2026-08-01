/**
 * Sidecar process launcher.
 *
 * When autoStartSidecar is enabled, this module checks whether the scheduler
 * sidecar is already reachable at the configured endpoint and, if not, spawns
 * it as a child process.  It returns a cleanup handle so the plugin can
 * terminate the sidecar on shutdown.
 */

import {spawn, type ChildProcess} from "node:child_process";
import {statSync} from "node:fs";
import {join} from "node:path";
import {fileURLToPath} from "node:url";

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
  env: Record<string, string>;
} | null {
  const python = pythonCommand();
  const projectRoot = resolveProjectRoot();
  if (!projectRoot) return null;

  const schedulerSrc = join(projectRoot, "services", "scheduler", "src");
  const url = new URL(endpoint);
  const host = url.hostname;
  const port = url.port || "8765";

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
export async function ensureSidecarRunning(
  opts: SidecarLaunchOptions,
): Promise<SidecarLauncherResult> {
  const {endpoint, command, healthPollMs, healthTimeoutMs, logger} = opts;
  const url = new URL(endpoint);
  const healthUrl = `${url.origin}/health/live`;

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
      const host = url.hostname;
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
  child.stderr?.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
    // Prevent unbounded memory growth for long-running sidecars
    if (stderr.length > 8192) {
      stderr = stderr.slice(-4096);
    }
  });

  child.on("exit", (code, signal) => {
    if (code !== 0 && code !== null) {
      logger.warn("sidecar exited unexpectedly", {code, signal, stderr: stderr.slice(-500)});
    }
  });

  child.on("error", (err) => {
    logger.error("sidecar process error", {error: err.message});
  });

  // 3. Wait for the sidecar to become healthy
  const deadline = Date.now() + healthTimeoutMs;

  while (Date.now() < deadline) {
    // Check if the child has already died
    if (child.exitCode !== null) {
      throw new Error(
        `sidecar exited with code ${child.exitCode} before becoming healthy. ` +
        `stderr: ${stderr.slice(-500)}`
      );
    }

    const healthy = await pingHealth(healthUrl, Math.min(healthPollMs, deadline - Date.now()));
    if (healthy) {
      logger.info("sidecar is healthy", {endpoint});
      unrefHealthyChild(child);
      return {
        child,
        cleanup: () => {
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
            if (child.exitCode === null) {
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

  // Timeout — kill the child and throw
  child.kill();
  throw new Error(
    `sidecar did not become healthy within ${healthTimeoutMs}ms. ` +
    `stderr: ${stderr.slice(-500)}`
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────

async function pingHealth(url: string, timeoutMs: number): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(1, timeoutMs));
  try {
    const response = await fetch(url, {
      method: "GET",
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
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
