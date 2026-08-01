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
privileges needed by eBPF. Setup gives the plugin an exact sudo command, and a
pre-agent hook waits for readiness to eliminate the old first-request race.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Process liveness |
| `GET /health/ready` | API readiness |
| `GET /metrics` | Prometheus metrics |
| `GET /v1/tools/recent` | Recent tool executions |
| `GET /v1/models` | OpenAI-compatible model discovery |
| `POST /v1/chat/completions` | Model proxy and tracing |

The health endpoints do not compile or attach probes on every request. Use
`python3 scripts/clawtune.py check` for kernel collector readiness.

## Resource prediction

The built-in `tool_resource` predictor learns from valid command artifacts and
configured OpenClaw JSONL traces. It persists its knowledge under the artifact
directory so exact command, argument-prefix, executable, and global evidence
can survive restarts. When evidence is missing, the prediction remains unknown
rather than inventing a value.

Predictions cover command latency buckets plus empirical conditional estimates
for latency, CPU, and memory. Compound shell commands retain independent clause
predictions because pipelines and conditional operators do not have one honest
composition rule.

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
