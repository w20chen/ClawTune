# Running ClawTune

The supported deployment has two host processes:

1. OpenClaw with the ClawTune plugin, running as the normal user;
2. the Scheduler sidecar, started through the repository wrapper with the
   kernel privileges required by eBPF.

OpenClaw continues to execute tools in its Docker sandbox. ClawTune does not
modify OpenClaw core, and placement advice remains advisory.

## Recommended Host Deployment

For interactive use, complete [installation and first run](getting-started.md),
then start one Gateway and attach the TUI:

```bash
openclaw gateway run
# in a second terminal
openclaw tui --session main
```

The plugin auto-starts the sidecar and waits for readiness. It derives the
checkout, `.venv`, `.env`, matching kernel build tree, and `sudo` arguments;
setup leaves `sidecarCommand` empty so a repository move cannot stale a
persisted absolute shell command.

A managed service without a controlling terminal should instead start the
long-lived sidecar explicitly because sudo cannot prompt there:

```bash
python3 scripts/clawtune.py sidecar
```

This wrapper supplies the exact environment that passed setup and is the
stable service-manager command for both openEuler/Kunpeng and x86 Linux.

## Health and Observability

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

## Service Manager Integration

For a persistent machine, wrap the same `sidecar` command in the site's service
manager and use the repository owner as the working user. There is no generic
service unit in the repository because working directories, account names, and
privilege policies are deployment-specific. The command itself invokes sudo,
so unattended operation requires a tightly scoped local policy for the
verified ClawTune launcher rather than a broad passwordless shell.

Keep the service bound to `127.0.0.1` unless authentication, firewalling, and
TLS termination have been designed for remote access. Provider credentials and
raw traces can contain sensitive data.

## SWE-Rebench Deployment

Run the batch wrapper instead of manually recreating sudo and environment
variables:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

It prepares a current bundle, starts one verified batch-owned Sidecar shared by
the selected task runtimes, and exports results. Kunpeng automatically uses
amd64 task images through QEMU; x86 uses native images.
`SWE_REBENCH_DOCKER_PLATFORM` is the explicit override for either host. See
[SWE-Rebench usage](../swe_rebench/README.md).

## Deep Research Bench Deployment

Run the Deep Research Bench wrapper the same way:

```bash
python3 scripts/clawtune.py drb --sample 1
```

It prepares the same runtime bundle and starts one host Sidecar. There is no
per-task image: the agent's tools run in one very basic Docker sandbox image
(`sandbox.image`, default `python:3.11-slim`) that is pulled once per batch.
Because that image is multi-arch, `drb` does not default the Docker platform to
`linux/amd64` on Kunpeng; export `SWE_REBENCH_DOCKER_PLATFORM` explicitly only
when the configured image needs it. Outputs live under
`deep_research_bench/.runtime/`. See
[Deep Research Bench usage](../deep_research_bench/README.md).

## Container-Only Development

`docker compose up --build scheduler` is useful for API development. It is not
the supported measurement deployment by itself: a container does not inherit
the host's matching headers, tracefs mount, perf access, and cgroup boundaries
simply because it is privileged.

## Security Notes

- The plugin's managed launcher rewrites shell execution and therefore requires
  explicit `securityBoundaryAccepted: true`; setup applies it.
- Keep the sidecar local and use `AGENT_SCHEDULER_TOKEN` if another local user
  must not call it.
- Never commit `.env`, model provider credentials, raw benchmark workspaces, or
  trace output.
- eBPF-disabled diagnostic output is incomplete and must not be presented as a
  successful ClawTune measurement.
