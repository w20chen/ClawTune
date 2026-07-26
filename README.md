# OpenClaw Agent Scheduler

OpenClaw Agent Scheduler is an OpenClaw plugin plus a Python sidecar. It records
OpenClaw model/tool traces and per-tool resource usage. It also includes a
SWE-Rebench batch runner.

## Preliminaries

Install OpenClaw (version 2026.7.1):

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://openclaw.ai/install.sh \
  | bash -s -- --install-method npm --version 2026.7.1
```

Clone this repository:

```bash
git clone git@github.com:w20chen/claw.git
```

## Install Local Packages

```bash
cd claw
python3 -m pip install -e "services/scheduler[dev]"

cd packages/openclaw-plugin
npm install
npm run build
cd ../..
```

## Run with OpenClaw

The recommended order is:

1. Start the sidecar.
2. Route OpenClaw model traffic through the sidecar proxy.
3. Install, enable, and configure the OpenClaw plugin.
4. Run OpenClaw.

This keeps the first plugin hook and the first model request pointed at a
healthy sidecar. Installing the plugin itself does not require an API key.

Before starting the sidecar, ensure that port 8765 is not occupied. If the port
is already in use by another process, you can forcefully release it with:

```bash
sudo lsof -t -i :8765 | xargs -r sudo kill -9
```

Start the sidecar and check readiness:

```bash
cp .env.example .env
python3 -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

```bash
curl http://127.0.0.1:8765/health/ready   # {"ready":true}
```

Route OpenClaw model traffic through the sidecar proxy.

If OpenClaw already has a `vllm` API-key profile, keep that key in OpenClaw and
only update the vLLM provider base URL and model to:

```text
http://127.0.0.1:8765/v1
deepseek-v4-flash
```

The sidecar forwards OpenClaw's `Authorization` header upstream by default, so
there is no separate plugin API key.

If OpenClaw does not already have a `vllm` API-key profile, `openclaw onboard`
requires one. This includes the common case where OpenClaw was previously
configured for DeepSeek directly, because the sidecar proxy is registered as a
local vLLM-compatible provider. Onboard vLLM once and point it at the sidecar:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local \
  --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<your provider API key>" \
  --custom-model-id "deepseek-v4-flash"
```

Install the plugin into OpenClaw, enable it, and patch its config.

```bash
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable agent-scheduler

LAUNCHER_PATH="$(command -v claw-launch)"
test -n "$LAUNCHER_PATH"

cat <<JSON5 | openclaw config patch --stdin
{
  plugins: {
    entries: {
      "agent-scheduler": {
        enabled: true,
        config: {
          endpoint: "http://127.0.0.1:8765",
          mode: "observe",
          failOpen: true,
          recordRawTrace: true,
          executionBackend: "managed-wrapper",
          launcherPath: "$LAUNCHER_PATH",
          enableCgroup: true,
          securityBoundaryAccepted: true
        }
      }
    }
  }
}
JSON5

openclaw plugins inspect agent-scheduler --runtime --json
```

`enableCgroup: true` is the plugin default, but Linux cgroup attribution also
requires the `claw-launch` process to see cgroup v2 and a writable cgroup root.
For local Linux runs, export these variables in the same shell that starts
`openclaw agent`:

```bash
test -f /sys/fs/cgroup/cgroup.controllers

# Optional cleanup after failed setup attempts. Only removes empty cgroups.
sudo find /sys/fs/cgroup/claw -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true

sudo mkdir -p /sys/fs/cgroup/claw
sudo chown "$USER:$USER" /sys/fs/cgroup/claw
sudo chown "$USER:$USER" \
  /sys/fs/cgroup/claw/cgroup.procs \
  /sys/fs/cgroup/claw/cgroup.threads \
  /sys/fs/cgroup/claw/cgroup.subtree_control

# Initialize cpuset when available, then enable controllers for child cgroups.
if [ -r /sys/fs/cgroup/cpuset.cpus.effective ]; then
  cat /sys/fs/cgroup/cpuset.cpus.effective | sudo tee /sys/fs/cgroup/claw/cpuset.cpus >/dev/null
fi
if [ -r /sys/fs/cgroup/cpuset.mems.effective ]; then
  cat /sys/fs/cgroup/cpuset.mems.effective | sudo tee /sys/fs/cgroup/claw/cpuset.mems >/dev/null
fi
for ctl in cpu cpuset io memory pids; do
  if grep -qw "$ctl" /sys/fs/cgroup/claw/cgroup.controllers 2>/dev/null; then
    echo "+$ctl" | sudo tee /sys/fs/cgroup/claw/cgroup.subtree_control >/dev/null || true
  fi
done

export CLAW_ENABLE_CGROUP=1
export CLAW_CGROUP_ROOT=/sys/fs/cgroup/claw
```

During setup/debugging, make cgroup failures explicit instead of silently
falling back to PID attribution:

```bash
export CLAW_CGROUP_REQUIRED=1
export CLAW_CGROUP_DEBUG=1
export CLAW_LAUNCH_DEBUG=1
```

With cgroup enabled, `/v1/tools/recent` should report
`"attribution_status":"cgroup-v2"` or traces should show
`"resources":{"scope":"cgroup"}` for managed `exec` tools. If it still reports
`"pid"` or `"unattributed"`, the launcher could not create or read the cgroup;
check the debug error, cgroup v2 availability, and write permission on
`/sys/fs/cgroup/claw`. Exit code `125` means `claw-launch` failed before the
payload command started; keep `CLAW_LAUNCH_DEBUG=1` enabled to print the
underlying exception.

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
