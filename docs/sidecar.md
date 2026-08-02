# Scheduler sidecar reference

The sidecar receives OpenClaw lifecycle events, proxies model requests, owns
the eBPF collector, records traces, and serves recent measurements/predictions.

## Start it

For an interactive agent run, plugin startup and cleanup are automatic:

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Run uname -a"
```

For a long-lived gateway or multiple invocations, start it explicitly:

```bash
python3 scripts/clawtune.py sidecar
```

Both paths use `.env`, listen on `127.0.0.1:8765`, and ask for the kernel
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
`python3 scripts/clawtune.py check` for kernel collector readiness.

## Resource prediction

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

The lattice learns only from eligible Stage-2 eBPF `ClauseObservation` values,
reusing the same validated static-clause identity and measured `latency_ms` as
the existing clause predictor. Results are exposed at
`prediction.tool_resource.lattice_time_predictions`, with one entry per
exec-producing static clause and all three algorithm outcomes under that entry.
Compound shell commands retain independent clause predictions: pipeline,
conditional, and sequential clause times are never combined into a synthetic
command duration.

The sidecar prebuilds lattice nodes during cold start and prepares the next
causally visible node state when a clause completes. Node generation retains
the latt default bound of six optional features when the corpus is small, then
reduces that bound deterministically to keep at most 4,096 generated nodes per
signature and target a 20,000-node-occurrence rebuild budget while always
retaining exact nodes. A non-exact shrinkage query
with more than 512 matching nodes reports `lattice_candidate_limit_exceeded`;
LOSO and max-cardinality remain available because they do not run the
quadratic dominance pass.

## Collection boundaries

Managed `exec` calls are released only after the sidecar has prepared the
collector and cgroup scope. Native sandbox file tools can be correlated through
Docker events; when they share a container cgroup, the trace explicitly marks
the shared attribution boundary.

The collector is required by default. Disabling it is useful only to isolate an
unrelated API/plugin problem; the resulting resource data is incomplete.

## Configuration and security

See [configuration](configuration.md) for normal settings. The complete
environment surface remains in `services/scheduler/src/agent_scheduler/config.py`
for developers.

- Bind locally unless remote authentication/TLS is deliberately configured.
- The proxy normally forwards OpenClaw's authorization header.
- Treat a custom sidecar shell command as trusted administrator input.
- Do not commit API keys or unredacted traces.
