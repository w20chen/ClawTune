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
AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://api.deepseek.com
AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY=sk-...
AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL=deepseek-chat
AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL=deepseek/deepseek-chat
```

The sidecar always uses the vendored `tool_resource` predictor. It can
cold-start from OpenClaw trace v6 JSONL files and native Stage-2 telemetry
artifacts. Trace v6 is call-level, so the adapter maps each eligible tool span
to one call-level pseudo-clause: `exec` spans use the primary command head and
internal tools use the tool name. If no usable evidence exists, prediction stays
`unknown` until new tool completions or managed-wrapper executions add
observations to the KB.

Native `tool_resource` Stage-2 collection is wired through the managed-wrapper
execution lifecycle. It needs a sandbox container id from OpenClaw or
`AGENT_SCHEDULER_SANDBOX_CONTAINER_ID`, an artifact directory, the configured
container executable, and the platform support expected by upstream
`tool_resource`.

Use `AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY` only when OpenClaw does not forward
the provider key you need.

Inspect output:

```bash
curl "http://127.0.0.1:8765/v1/tools/recent?limit=5"
ls data/traces
python tools/inspect_trace.py data/traces/<trace-file>.jsonl --all --details
```
