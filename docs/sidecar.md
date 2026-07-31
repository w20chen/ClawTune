# Sidecar Usage

Start:

```bash
cp .env.example .env
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

Health:

```bash
curl http://127.0.0.1:8765/health/live
curl http://127.0.0.1:8765/health/ready
```

Useful endpoints:

- `GET /v1/tools/recent`
- `GET /metrics`
- `GET /v1/models`
- `POST /v1/chat/completions`

Important `.env` values:

```bash
AGENT_SCHEDULER_DB_PATH=data/openclaw-trace.sqlite3
AGENT_SCHEDULER_TRACE_DIR=data/traces
AGENT_SCHEDULER_TOOL_RESOURCE_TRACES=data/traces
AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_TRACES=data/tool-resource-stage2
AGENT_SCHEDULER_TOOL_RESOURCE_LATENCY_BUCKETS_MS=100,500,2000,10000
AGENT_SCHEDULER_TOOL_RESOURCE_REPO=openclaw
AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR=data/tool-resource
AGENT_SCHEDULER_TOOL_RESOURCE_CONTAINER_EXECUTABLE=docker
AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=true
AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://api.deepseek.com
AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL=deepseek-v4-flash
AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL=deepseek/deepseek-v4-flash
```

The sidecar uses the built-in `tool_resource` predictor. It can
cold-start from OpenClaw trace v6 JSONL files and native Stage-2 telemetry
artifacts. Trace v6 is call-level, so the adapter maps each eligible tool span
to one call-level pseudo-clause: `exec` spans use the primary command head and
internal tools use the tool name. If no usable evidence exists, prediction stays
`unknown` until new tool completions or managed-wrapper executions add
observations to the KB.

The clause KB is persisted as `clause-resource-kb.json` under
`AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR` (or
`<trace_dir>/tool-resource` by default). On cold start without an existing
snapshot, configured OpenClaw trace v6 and Stage-2 artifacts seed both public
bin/global priors and repo exact/argv-prefix/bin nodes, so prefix backoff can
survive sidecar restarts. The sidecar writes that merged snapshot immediately
after cold start and after every successful online KB update.

Tool decisions and trace span starts include the full non-MLP prediction
payload under `prediction.tool_resource`: the clause latency bucket predictor,
the continuous empirical conditional-p90 predictions for latency, CPU, and
memory, and a `prediction_algorithms` section that explicitly lists enabled
non-MLP algorithms while marking `tool_resource.mlp` as excluded.
`clause_predictions` reports each executable shell clause independently. A
compound command intentionally keeps its command-level `prediction` null,
because `&&`, `||`, and pipelines do not share one valid bucket-composition
rule; its evidence-backed clause predictions remain available to policy and
coverage audits. Shell builtins such as `cd` are retained in `clause_bins` for
parse transparency but are not treated as missing executable telemetry.
When no persisted or seeded clause latency evidence exists, the native payload
still appears with `unavailable_reason: "no_clause_latency_evidence"` so
operators can distinguish "predictor ran but had no evidence" from integration
failure.

Native `tool_resource` Stage-2 collection is the primary managed-wrapper
execution path. It needs a sandbox container id from OpenClaw or
`AGENT_SCHEDULER_SANDBOX_CONTAINER_ID`, an artifact directory, the configured
container executable, and the platform support expected by upstream
`tool_resource`.

When `AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=true`, managed-wrapper
claims fail closed if the Stage-2 collector cannot start before the payload
command is released. This prevents silently falling back to whole-tool
process/cgroup sampling for shell commands that need clause-level telemetry.

For managed-wrapper executions with cgroup profiling, `claw-launch` reports
the prepared cgroup to the sidecar before releasing the payload command. This
lets the realtime monitor start sampling before short-lived commands finish;
the launcher then updates the same execution with the real child PID once the
payload process exists.

For native sandbox file tools (`read`, `write`, `edit`, and `apply_patch`),
the optional Docker observer subscribes to both `exec_create` and `exec_start`.
It correlates the Docker exec command with an active tool and rebinds the
monitor to the live host PID plus descendants. Because Docker exec processes
share the container cgroup, exact matches are recorded as `scope:
process_tree`, `source: docker-events`, and `attribution_source:
docker-exec-pid`. If the process exits before a PID baseline can be sampled,
the sidecar retains the discovered shared sandbox cgroup with
`coverage_reason: shared_sandbox_container`.

The LLM proxy is always enabled for plugin use. By default it forwards the
provider key already configured in OpenClaw via the request `Authorization`
header. Set `AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE` only when you
intentionally want the sidecar to use a different upstream key.

Inspect output:

```bash
curl "http://127.0.0.1:8765/v1/tools/recent?limit=5"
ls data/traces
python tools/inspect_trace.py data/traces/<trace-file>.jsonl --all --details
```

## Security

- The plugin runs as a normal OpenClaw plugin.
- This project does not modify OpenClaw core.
- Use `mode: "observe"` unless you intentionally want enforcement behavior.
- `managed-wrapper` rewrites `exec` commands and therefore requires
  `securityBoundaryAccepted: true`.
- Do not store provider API keys in committed config files. Normal plugin runs
  should use the key already configured in OpenClaw; the sidecar forwards
  OpenClaw's `Authorization` header by default. Use `LLM_API_KEY` only for
  SWE-Rebench automation, and use
  `AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE` only for an intentional
  sidecar override.
