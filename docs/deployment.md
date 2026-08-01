# Running ClawTune

The supported deployment has two host processes:

1. OpenClaw with the ClawTune plugin, running as the normal user;
2. the Scheduler sidecar, started through the repository wrapper with the
   kernel privileges required by eBPF.

OpenClaw continues to execute tools in its Docker sandbox. ClawTune does not
modify OpenClaw core, and placement advice remains advisory.

## Recommended host deployment

Complete [installation and first run](getting-started.md), then start:

```bash
python3 scripts/clawtune.py sidecar
```

The wrapper supplies the exact `.venv`, kernel build tree, `.env`, and clean
executable path that passed setup. It is the stable deployment command for both
openEuler/Kunpeng and x86 Linux.

Interactive OpenClaw use auto-starts this process and waits for readiness. The
plugin derives the checkout, `.venv`, `.env`, matching kernel build tree, and
`sudo` arguments when it launches; setup leaves `sidecarCommand` empty so a
repository move cannot stale a persisted absolute shell command. A managed
service without a controlling terminal should start the long-lived sidecar
explicitly because sudo cannot prompt there.

## Health and observability

```bash
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
curl -fsS http://127.0.0.1:8765/metrics
curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
```

Health endpoints show that the API is available. Run
`python3 scripts/clawtune.py check` after a kernel, BCC, or Clang update to
verify the complete collector with a real process. ClawTune accepts a health
response only when it carries the expected `clawtune-scheduler` service and
`scheduler.health.v1` schema identity; another process on port 8765 is treated
as a conflict.

## Service manager integration

For a persistent machine, wrap the same `sidecar` command in the site's service
manager and use the repository owner as the working user. There is no generic
service unit in the repository because working directories, account names, and
privilege policies are deployment-specific. The command itself invokes sudo,
so unattended operation requires a tightly scoped local policy for the
verified ClawTune launcher rather than a broad passwordless shell.

Keep the service bound to `127.0.0.1` unless authentication, firewalling, and
TLS termination have been designed for remote access. Provider credentials and
raw traces can contain sensitive data.

## SWE-Rebench deployment

Run the batch wrapper instead of manually recreating sudo and environment
variables:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

It prepares a current bundle, starts the verified sidecar for each task, and
exports results. Kunpeng automatically uses amd64 task images through QEMU;
x86 uses native images. `SWE_REBENCH_DOCKER_PLATFORM` is the explicit override
for either host. See [SWE-Rebench usage](../swe_rebench/README.md).

## Container-only development

`docker compose up --build scheduler` is useful for API development. It is not
the supported measurement deployment by itself: a container does not inherit
the host's matching headers, tracefs mount, perf access, and cgroup boundaries
simply because it is privileged.

## Security notes

- The plugin's managed launcher rewrites shell execution and therefore requires
  explicit `securityBoundaryAccepted: true`; setup applies it.
- Keep the sidecar local and use `AGENT_SCHEDULER_TOKEN` if another local user
  must not call it.
- Never commit `.env`, model provider credentials, raw benchmark workspaces, or
  trace output.
- eBPF-disabled diagnostic output is incomplete and must not be presented as a
  successful ClawTune measurement.
