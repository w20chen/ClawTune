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
AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://api.deepseek.com
AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL=deepseek-v4-flash
AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL=deepseek/deepseek-v4-flash
```

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
