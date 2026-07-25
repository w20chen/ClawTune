# OpenClaw Agent Scheduler

OpenClaw Agent Scheduler is an OpenClaw plugin plus a Python sidecar. It records
OpenClaw model/tool traces and per-tool resource usage. It also includes a
SWE-Rebench batch runner.

## Install

```bash
python -m pip install -e "services/scheduler[dev]"

cd packages/openclaw-plugin
npm install
npm run build
cd ../..
```

## Run With OpenClaw

Start the sidecar:

```bash
cp .env.example .env
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

Before starting the sidecar, please ensure that port 8765 is not occupied. If the port is already in use by another process, you can forcefully release it with the following command:

```bash
sudo lsof -t -i :8765 | xargs -r sudo kill -9
```

Install the plugin:

```bash
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable hardware-scheduler
```

Route OpenClaw model traffic through the sidecar proxy. Use the same provider
API key you normally configure in OpenClaw; the sidecar forwards OpenClaw's
`Authorization` header by default, so there is no separate plugin API key.

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local \
  --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<your existing provider API key>" \
  --custom-model-id "deepseek-v4-flash"
```

Configure the plugin. Replace `launcherPath` with your absolute `claw-launch`
path:

```bash
cat <<'JSON5' | openclaw config patch --stdin
{
  plugins: {
    entries: {
      "hardware-scheduler": {
        enabled: true,
        config: {
          endpoint: "http://127.0.0.1:8765",
          mode: "observe",
          failOpen: true,
          recordRawTrace: true,
          executionBackend: "managed-wrapper",
          launcherPath: "/absolute/path/to/claw-launch",
          securityBoundaryAccepted: true
        }
      }
    }
  }
}
JSON5
```

Run:

```bash
openclaw agent --local --agent main --model "vllm/deepseek-v4-flash" \
  --message "Use the shell to run: python -c 'print(\"trace-ok\")'. Then summarize the result."
```

Inspect:

```bash
curl "http://127.0.0.1:8765/v1/tools/recent?limit=5"
ls data/traces
python tools/inspect_trace.py data/traces/<trace-file>.jsonl --all --details
```

## Run SWE-Rebench In Batch

```bash
cp swe_rebench/config.example.yaml swe_rebench/config.yaml
# SWE-Rebench is automated and does not read your host OpenClaw key.
# Set LLM_API_KEY, edit llm.api_key, or use swe_rebench/llm_api_key.txt.

python -m swe_rebench.runner prepare --config swe_rebench/config.yaml
python -m swe_rebench.discover --sample 20 --out swe_rebench/tasks.json

python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 10 \
  --parallelism 4 \
  --export
```

Useful selectors:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --instance-ids django__django-12345,sympy__sympy-67890

python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --repo django/django \
  --sample 5 \
  --dry-run
```

## More

- Normal OpenClaw guide: [docs/operator-guide.md](docs/operator-guide.md)
- SWE-Rebench guide: [swe_rebench/README.md](swe_rebench/README.md)
- Deployment: [docs/deployment.md](docs/deployment.md)
- Troubleshooting: [docs/operator-guide.md#troubleshooting](docs/operator-guide.md#troubleshooting)

## Validate

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root

cd services/scheduler
python -m pytest tests -q

cd ../../packages/openclaw-plugin
npm test
npm run typecheck
```
