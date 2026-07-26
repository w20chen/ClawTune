# Run OpenClaw With Tracing

Use this guide for normal OpenClaw runs.

## 1. Install Local Packages

```bash
python -m pip install -e "services/scheduler[dev]"

cd packages/openclaw-plugin
npm install
npm run build
cd ../..
```

Check the launcher:

```bash
claw-launch --help
```

Recommended runtime order:

1. Start the sidecar.
2. Route OpenClaw model traffic through the sidecar proxy.
3. Install, enable, and configure the OpenClaw plugin.
4. Run OpenClaw.

Installing the plugin itself does not require an API key, but doing it after
the sidecar readiness check keeps the first plugin hook pointed at a healthy
endpoint.

## 2. Start Sidecar

```bash
cp .env.example .env
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

Health:

```bash
curl http://127.0.0.1:8765/health/ready
```

## 3. Configure OpenClaw Model Proxy

If OpenClaw already has a `vllm` API-key profile, keep that key in OpenClaw and
only update the vLLM provider base URL and model to:

```text
http://127.0.0.1:8765/v1
deepseek-v4-flash
```

The sidecar LLM proxy is always on while using the plugin and forwards
OpenClaw's `Authorization` header upstream by default, so the plugin does not
need a second API key.

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

For OpenRouter or another OpenAI-compatible upstream, edit `.env` and restart
the sidecar. Keep using the provider key stored in OpenClaw unless you
intentionally need an override.

```bash
AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL=deepseek-v4-flash
AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL=deepseek/deepseek-v4-flash
# Optional advanced override:
# AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE=sk-...
```

## 4. Install And Configure Plugin

```bash
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable agent-scheduler
```

Patch OpenClaw config.

```bash
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
          securityBoundaryAccepted: true
        }
      }
    }
  }
}
JSON5

openclaw plugins inspect agent-scheduler --runtime --json
```

Debug-only fallback:

```json5
executionBackend: "hook-only"
```

## 5. Run

```bash
openclaw agent --local --agent main --model "vllm/deepseek-v4-flash" \
  --message "Use the shell to run: python -c 'print(\"trace-ok\")'. Then summarize the result."
```

## SWE-Rebench Host Sandbox Mode

SWE-Rebench can also run with OpenClaw on the host while OpenClaw's Docker
sandbox executes tools:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --runtime-mode host-openclaw-sandbox \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --parallelism 1 \
  --export
```

This mode exports `/testbed` from the task image into a host workspace, starts a
host sidecar, creates an isolated OpenClaw home/config for the task, and mounts
the workspace at `/workspace` inside the OpenClaw sandbox.

`exec` remains managed by `claw-launch`. Internal tools are attributed to the
shared sandbox container cgroup when the runner can discover it through
`openclaw sandbox list --json` and Docker inspect. These spans use
`coverage_reason: "shared_sandbox_container"` because this is sandbox container
time-window attribution, not a strict per-tool PID/cgroup.

## 7. Inspect

```bash
curl "http://127.0.0.1:8765/v1/tools/recent?limit=5"
curl http://127.0.0.1:8765/metrics
ls data/traces
python tools/inspect_trace.py data/traces/<trace-file>.jsonl --all --details
```

## Troubleshooting

- No full LLM content: confirm OpenClaw logs use `http://127.0.0.1:8765/v1`.
- Tool args/results are null: confirm `recordRawTrace: true`.
- Resource usage is `unattributed`: use `managed-wrapper` and an absolute
  `launcherPath`.
- Per-tool cgroup migration fails during normal `openclaw agent ...` usage:
  `claw-launch` automatically retries the payload in a transient user systemd
  scope when cgroup profiling is enabled. Set `CLAW_CGROUP_AUTO_SYSTEMD=0` to
  disable that retry and use the process-tree fallback.
- `claw-launch` exits 125 with `cgroup_join_failed ... Permission denied`:
  cgroup v2 is present, but delegation is incomplete. Owning the destination
  `cgroup.procs` is not sufficient if the launcher process starts outside the
  delegated tree; cgroup v2 also checks migration permission through the source
  and destination common ancestor. Use the README probe to verify this, then
  start OpenClaw inside a delegated cgroup, for example with
  `systemd-run --user --scope -p Delegate=yes ... openclaw agent ...`. If a
  delegated scope is unavailable, unset `CLAW_CGROUP_REQUIRED` or set it to `0`
  and rely on PID attribution until the host/container cgroup setup is fixed.
- `claw-launch` not found: reinstall the scheduler package and patch the
  absolute launcher path.
- On Windows PowerShell, use `npm.cmd` or `openclaw.cmd` if `.ps1` shims are
  blocked.
