# ClawTune Sidecar Reference

The sidecar receives OpenClaw lifecycle events, proxies model requests, owns
the eBPF collector, records traces, and serves predictions and recent
measurements.

For setup use [getting-started](getting-started.md); for settings use
[configuration](configuration.md); for field semantics use the
[JSON Schemas](../contracts/) and [trace reference](trace-schema.md).

## Lifecycle

Normally the plugin starts the sidecar and waits for an identified ready
response. A Gateway reuses it across sessions and runs:

```bash
# terminal 1
openclaw gateway run

# terminal 2
openclaw tui --session main
```

Start it explicitly only when a service manager or non-interactive environment
must own its privileged lifetime:

```bash
python3 scripts/clawtune.py sidecar
```

All supported paths load `.env` and bind to `127.0.0.1:8765` by default. The
plugin keeps `sidecarCommand` empty and resolves the current checkout, `.venv`,
kernel build tree, and sudo launch command at runtime.

## Public endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Identified process liveness |
| `GET /health/ready` | Identified API readiness |
| `GET /v1/status` | Scheduler status |
| `GET /metrics` | Prometheus metrics |
| `GET /v1/tools/recent` | Recent correlated tool executions |
| `GET /v1/models` | OpenAI-compatible model discovery |
| `POST /v1/chat/completions` | Model proxy and tracing |
| `POST /v1/decisions/tool` | Tool prediction/admission decision |
| `POST /v1/events/tool-completed` | Tool completion and learning |
| `POST /v1/events/model` | Model lifecycle event |
| `POST /v2/executions` | Managed-execution registration |

Managed-execution claim, scope, telemetry, exit, runtime-scope, and drain
routes are also versioned under `/v1` or `/v2` and are consumed by the plugin
and launcher. Their request/response schemas under `contracts/` are
authoritative.

Both health responses include `service: clawtune-sidecar` and
`schema_version: clawtune.health.v1`. The launcher checks those values rather
than accepting any listener on the port.

```bash
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
```

Health does not compile or attach probes. Use
`python3 scripts/clawtune.py check` after kernel, BCC, or Clang changes.

## Prediction and learning

The predictor learns only from quality-gated command and tool observations.
Missing evidence produces an explicit unavailable result. The main outputs are:

- call-load distributions and continuous duration/CPU/memory estimates;
- clause duration from `shrinkage`, `loso`, and `max_cardinality`;
- lattice CPU and memory predictions;
- Tool-level IPC, LLC read MPKI, and LLC miss-rate predictions;
- advisory admission and placement metadata.

The detailed algorithms, units, bucket composition, and quality gates live in
[call-load prediction](call-load-prediction.md),
[lattice resources](lattice-resources.md), and
[PMU profiling](pmu-profiling.md). They are not duplicated here.

### Asynchronous KB writer

Completion handling updates accepted in-memory Runtime/Trie observations under
one re-entrant KB lock, then enqueues a lightweight persistence notification.
A dedicated single writer coalesces notifications, prepares lattice changes,
writes changed snapshots atomically, and commits the three-file generation
through `CURRENT`. Readers continue using the last prepared lattice while a
new lattice is built.

A runtime drain normally waits for active executions, deferred finalizers, and
trace operations. Its `flush_kb=false` mode intentionally skips the global
persistence barrier; the common concurrent benchmark uses this at task
boundaries so one completed task does not stall its peers. After all benchmark
workers finish, one `flush_kb=true` drain places a barrier after all queued
updates. Failure of that barrier fails the run; the sidecar is not reported as
durably complete.

## Collection boundaries

Managed `exec` calls are released only after the sidecar has prepared their
collector and execution scope. Repository benchmark runs require an exclusive
execution cgroup and valid eBPF clause telemetry. Native sandbox file tools can
be correlated through Docker events; shared cgroup or process-tree attribution
is labeled explicitly.

The collector is required by default. Disabling it is only a diagnostic mode;
its resource output must not be presented as a complete ClawTune measurement.
Placement remains advisory in this MVP.

## Deployment and security

`docker compose up --build sidecar` is useful for API development, not accepted
host measurement: a container does not automatically inherit matching kernel
headers, tracefs, perf access, or the needed cgroup boundaries.

For a persistent service, wrap `python3 scripts/clawtune.py sidecar` in the
site's service manager with a narrowly scoped privilege policy. Keep the
service on loopback unless authentication, firewalling, and TLS have been
designed for remote use.

- Set `CLAWTUNE_TOKEN` when another local user must not call the sidecar.
- Treat a custom sidecar shell command as administrator-controlled input.
- Do not commit API keys, unredacted traces, or benchmark workspaces.
- Setup accepts the plugin's managed-execution security boundary explicitly.
