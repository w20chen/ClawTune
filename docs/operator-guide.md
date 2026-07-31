# Run OpenClaw with Tracing

This guide covers model proxy setup, smoke testing, and troubleshooting.
For installation, sidecar startup, and plugin configuration, follow the
[README](../README.md) quickstart first, then return here.

## 1. Configure OpenClaw Model Proxy

Route OpenClaw model traffic through the sidecar proxy. Any OpenAI-compatible
provider works. The sidecar forwards OpenClaw's `Authorization` header
upstream by default. Two common setups:

**DeepSeek (direct API):** Update the vLLM provider base URL and model:

```text
http://127.0.0.1:8765/v1
deepseek-v4-flash
```

Or onboard a new profile:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local \
  --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<your DeepSeek API key>" \
  --custom-model-id "deepseek-v4-flash"
```

**OpenRouter (or any OpenAI-compatible upstream):** Set these in `.env` and
restart the sidecar:

```bash
AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL=deepseek-v4-flash
AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL=deepseek/deepseek-v4-flash
```

Set `AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE` only for an intentional
sidecar API key override. Replace `deepseek-v4-flash` above with your
provider's model ID.

## 2. SWE-Rebench Host Sandbox

To run SWE-Rebench with OpenClaw on the host while OpenClaw's Docker
sandbox executes tools:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --runtime-mode host-openclaw-sandbox \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
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

## 3. Smoke Test

After completing the README quickstart and model proxy setup above, run this
to verify the full pipeline end-to-end:

```bash
openclaw agent --local --agent main --model "vllm/<your-model>" \
  --message "Run: python -c 'print(\"hello\")'. Then say 'ok'."
```

Then check that the trace was recorded:

```bash
ls data/traces/*.jsonl
python tools/inspect_trace.py data/traces/*.jsonl --all --details
```

What each check proves:

| Check | What it validates |
|---|---|
| `openclaw agent ...` succeeds | OpenClaw CLI + plugin hooks are functional |
| `data/traces/*.jsonl` exists | Sidecar is receiving and writing span events |
| Tool span contains `resources` | `managed-wrapper` is intercepting `exec` and the launcher is sampling cgroup/PID |
| LLM span has `input.messages` | Model proxy is capturing full request/response content |

If any of these fail, see Troubleshooting below.

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
  and destination common ancestor.

  **Diagnose:** Confirm cgroup v2 and the permission failure mode:

  ```bash
  test -f /sys/fs/cgroup/cgroup.controllers   # confirm cgroup v2

  probe=/sys/fs/cgroup/claw/probe-$$
  sudo mkdir -p "$probe"
  sudo chown "$USER:$USER" "$probe" "$probe/cgroup.procs"
  echo $$ > "$probe/cgroup.procs"
  ```

  If the probe prints `Permission denied`, do not use `/sys/fs/cgroup/claw`
  as a shared host-level root. Instead, start OpenClaw inside a delegated
  cgroup scope:

  ```bash
  systemd-run --user --scope -p Delegate=yes bash -lc '
    set -euo pipefail
    self_cg="/sys/fs/cgroup$(awk -F: '\''$1=="0"{print $3}'\'' /proc/self/cgroup)"
    export CLAW_CGROUP_ROOT="$self_cg/claw"
    export CLAW_ENABLE_CGROUP=1
    export CLAW_CGROUP_REQUIRED=1
    export CLAW_CGROUP_DEBUG=1
    export CLAW_LAUNCH_DEBUG=1
    exec openclaw agent --local --agent main --model "vllm/<your-model>" \
      --message "Use the shell to run: python -c '\''print(\"trace-ok\")'\''. Then summarize the result."
  '
  ```

  `CLAW_CGROUP_ROOT` must be computed inside the `systemd-run` shell because
  each delegated scope has its own cgroup path. For normal interactive use,
  keep the same wrapper shape and replace the `--message ...` portion.

  With cgroup enabled, `/v1/tools/recent` should report
  `"attribution_status":"cgroup-v2"` or traces should show
  `"resources":{"scope":"cgroup"}` for managed `exec` tools.

  **Fallback:** If systemd user scopes are unavailable, disable cgroup and
  use PID attribution until the host/container can provide a delegated cgroup
  tree:

  ```bash
  export CLAW_ENABLE_CGROUP=0
  export CLAW_CGROUP_REQUIRED=0
  ```

  Exit code `125` means `claw-launch` failed before the payload command
  started. Keep `CLAW_LAUNCH_DEBUG=1` enabled while debugging to print the
  underlying exception.
- `claw-launch` not found: reinstall the scheduler package and patch the
  absolute launcher path.
- On Windows PowerShell, use `npm.cmd` or `openclaw.cmd` if `.ps1` shims are
  blocked.
