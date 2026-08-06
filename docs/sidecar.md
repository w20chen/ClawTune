# Scheduler Sidecar Reference

The sidecar receives OpenClaw lifecycle events, proxies model requests, owns
the eBPF collector, records traces, and serves recent measurements/predictions.

## Start the Sidecar

Normally, do not start the sidecar separately. The plugin starts it on demand
inside whichever OpenClaw runtime owns the work. With a Gateway, the same
sidecar is reused across sessions and runs until the Gateway exits:

```bash
# terminal 1
openclaw gateway run

# terminal 2
openclaw tui --session main
```

For a one-shot embedded run, plugin startup and cleanup follow that process:

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Run uname -a"
```

Start the sidecar explicitly only when OpenClaw is non-interactive and cannot
perform the required privilege prompt, or when a service manager deliberately
owns its lifetime:

```bash
python3 scripts/clawtune.py sidecar
```

All paths use `.env`, listen on `127.0.0.1:8765`, and require the kernel
privileges needed by eBPF. The plugin keeps `sidecarCommand` empty and resolves
the current checkout, `.venv`, matching kernel build tree, and `sudo` launch at
runtime. A pre-agent hook waits for an identified ClawTune health response to
eliminate the old first-request race.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Identified process liveness |
| `GET /health/ready` | Identified API readiness |
| `GET /metrics` | Prometheus metrics |
| `GET /v1/tools/recent` | Recent tool executions |
| `GET /v1/models` | OpenAI-compatible model discovery |
| `POST /v1/chat/completions` | Model proxy and tracing |

Both health responses include `service: clawtune-scheduler` and
`schema_version: scheduler.health.v1`. The launcher checks those fields instead
of treating any HTTP server on port 8765 as ClawTune. An unrelated listener is
therefore a port conflict and startup stops with an actionable error. The
public response contract is `contracts/health.schema.json`.

The health endpoints do not compile or attach probes on every request. Use
`python3 scripts/clawtune.py check` after a kernel, BCC, or Clang update to
verify the complete collector with a real process.

Quick checks:

```bash
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
curl -fsS http://127.0.0.1:8765/metrics
curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
```

## Resource Prediction

The built-in `tool_resource` predictor learns from valid command artifacts and
configured OpenClaw JSONL traces. It persists its knowledge under the artifact
directory so exact command, argument-prefix, executable, and global evidence
can survive restarts. When evidence is missing, the prediction remains unknown
rather than inventing a value.

Predictions cover command latency buckets plus empirical conditional estimates
for latency, CPU, and memory. They also include three clause-level point-time
predictors: `shrinkage`, `loso`, and `max_cardinality`. These algorithms share
one independent, flat lattice KB; common nodes and nodes carrying a repository
feature live in the same node map rather than separate public/repo layers.

The lattice learns only from eligible eBPF `ClauseObservation` values,
reusing the same validated static-clause identity and measured `latency_ms` as
the existing clause predictor. Results are exposed at
`prediction.tool_resource.lattice_time_predictions`, with one entry per
exec-producing static clause and all three algorithm outcomes under that entry.
Compound shell commands retain independent clause predictions: pipeline,
conditional, and sequential clause times are never combined into a synthetic
command duration. The lattice is prebuilt during cold start and updated as
clauses complete; large corpora are bounded deterministically, and a query
that exceeds the candidate limit reports
`lattice_candidate_limit_exceeded` (the other two algorithms remain
available).

## Collection Boundaries

Managed `exec` calls are released only after the sidecar has prepared the
collector and cgroup scope. Native sandbox file tools can be correlated through
Docker events; when they share a container cgroup, the trace explicitly marks
the shared attribution boundary.

The collector is required by default. Disabling it is useful only to isolate an
unrelated API/plugin problem; the resulting resource data is incomplete.

ClawTune observes by default: scheduling/placement recommendations do not
forcibly move work in the current release.

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

## Container-Only Development

`docker compose up --build scheduler` is useful for API development. It is not
the supported measurement deployment by itself: a container does not inherit
the host's matching headers, tracefs mount, perf access, and cgroup boundaries
simply because it is privileged.

## Configuration and Security

See [configuration](configuration.md) for normal settings. The complete
environment surface remains in `services/scheduler/src/agent_scheduler/config.py`
for developers.

- Bind locally unless remote authentication/TLS is deliberately configured.
- The proxy normally forwards OpenClaw's authorization header.
- Treat a custom sidecar shell command as trusted administrator input.
- Do not commit API keys or unredacted traces.
- Setup applies the plugin's `securityBoundaryAccepted: true` because the
  managed launcher rewrites shell execution.
- Keep the sidecar local and use `AGENT_SCHEDULER_TOKEN` if another local user
  must not call it.
- Never commit `.env`, model provider credentials, raw benchmark workspaces, or
  trace output.
- eBPF-disabled diagnostic output is incomplete and must not be presented as a
  successful ClawTune measurement.
