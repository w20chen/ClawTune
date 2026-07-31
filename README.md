# OpenClaw Agent Scheduler

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%E2%89%A52026.7.1-6e40c9.svg)](https://openclaw.ai/)
[![Node.js](https://img.shields.io/badge/Node.js-24-green.svg?logo=node.js&logoColor=white)](https://nodejs.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/w20chen/claw)

OpenClaw Agent Scheduler is an OpenClaw plugin plus a Python sidecar. It records
OpenClaw model/tool traces and per-tool resource usage. It also includes a
SWE-Rebench batch runner.

**Platform:** This guide works on x86_64 Linux. ARM/Kunpeng users must also
complete [docs/arm-qemu.md](docs/arm-qemu.md) before running SWE-Rebench —
task images are x86_64-only and require QEMU emulation.

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

For the full step-by-step guide with troubleshooting, see
[docs/operator-guide.md](docs/operator-guide.md). Quick start:

```bash
# 1. Start sidecar
cp .env.example .env
python3 -m agent_scheduler.main --host 127.0.0.1 --port 8765 &

# 2. Configure OpenClaw to route through sidecar proxy
#    Set provider base URL to http://127.0.0.1:8765/v1, model to deepseek-v4-flash

# 3. Install and configure plugin
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable agent-scheduler
LAUNCHER_PATH="$(command -v claw-launch)"
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

# 4. Run
openclaw agent --local --agent main --model "vllm/deepseek-v4-flash" \
  --message "Use the shell to run: python -c 'print(\"trace-ok\")'. Then summarize the result." \
  --session-key "my-session"
```

The sidecar must be running before the plugin hooks fire and before the first
model request. See [operator-guide.md](docs/operator-guide.md) for model proxy
configuration (vLLM onboarding, OpenRouter, API key setup) and smoke test
verification.

### Linux Cgroup

`enableCgroup: true` is the plugin default. For reliable cgroup-v2 attribution
on local Linux, start OpenClaw inside a delegated cgroup scope and set
`CLAW_CGROUP_ROOT` under that scope. This avoids the common
`cgroup_join_failed ... Permission denied` failure caused by trying to move a
process from an unrelated host cgroup into `/sys/fs/cgroup/claw`.

First confirm that the host uses cgroup v2:

```bash
test -f /sys/fs/cgroup/cgroup.controllers
```

Then run OpenClaw through a delegated systemd user scope:

```bash
systemd-run --user --scope -p Delegate=yes bash -lc '
  set -euo pipefail
  self_cg="/sys/fs/cgroup$(awk -F: '\''$1=="0"{print $3}'\'' /proc/self/cgroup)"
  export CLAW_CGROUP_ROOT="$self_cg/claw"
  export CLAW_ENABLE_CGROUP=1
  export CLAW_CGROUP_REQUIRED=1
  export CLAW_CGROUP_DEBUG=1
  export CLAW_LAUNCH_DEBUG=1
  exec openclaw agent --local --agent main --model "vllm/deepseek-v4-flash" \
    --message "Use the shell to run: python -c '\''print(\"trace-ok\")'\''. Then summarize the result."
'
```

Expected result:

```text
trace-ok
```

For normal interactive use, keep the same wrapper shape and replace the
`--message ...` portion with your usual OpenClaw invocation. `CLAW_CGROUP_ROOT`
must be computed inside the `systemd-run` shell because each delegated scope has
its own cgroup path.

With cgroup enabled, `/v1/tools/recent` should report
`"attribution_status":"cgroup-v2"` or traces should show
`"resources":{"scope":"cgroup"}` for managed `exec` tools.

`claw-launch` also has an automatic systemd fallback for normal
`openclaw agent ...` usage. If a per-tool cgroup can be created but the child
process cannot be migrated into it, the launcher retries the payload in a
transient user systemd scope with `Delegate=yes` and reports that scope's
cgroup to the sidecar. Set `CLAW_CGROUP_AUTO_SYSTEMD=0` to disable this retry
and use the process-tree fallback.

### Cgroup Troubleshooting

On cgroup v2, owning the destination `cgroup.procs` file is not enough. The
kernel also checks migration permission through the source and destination
common ancestor. This probe confirms the failure mode:

```bash
probe=/sys/fs/cgroup/claw/probe-$$
sudo mkdir -p "$probe"
sudo chown "$USER:$USER" "$probe" "$probe/cgroup.procs"
echo $$ > "$probe/cgroup.procs"
```

If the probe prints `Permission denied`, do not use `/sys/fs/cgroup/claw` as a
shared host-level root for local shell launches. Use the delegated
`systemd-run --user --scope -p Delegate=yes` command above.

If systemd user scopes are unavailable, use PID attribution until the
host/container can provide a delegated cgroup tree:

```bash
export CLAW_ENABLE_CGROUP=0
export CLAW_CGROUP_REQUIRED=0
```

Exit code `125` means `claw-launch` failed before the payload command started.
Keep `CLAW_LAUNCH_DEBUG=1` enabled while debugging to print the underlying
exception.

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

sudo -E env "PATH=$PATH" "$(command -v python3)" \
  -m swe_rebench.runner run \
  --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export \
  --runtime-mode host-openclaw-sandbox
```

Useful selectors:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --instance-ids 12rambau__sepal_ui-411,12rambau__sepal_ui-501

python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --repo django/django \
  --sample 5 \
  --dry-run
```

## Supported Workflows

- Run the sidecar locally on `127.0.0.1:8765`.
- Install and enable the `agent-scheduler` OpenClaw plugin.
- Route OpenClaw model traffic through the sidecar OpenAI-compatible proxy.
- Record schema v6 JSONL traces under `data/traces`.
- Record hook-visible tool args/results with `recordRawTrace: true`.
- Attribute `exec` resource usage with `executionBackend: "managed-wrapper"`.
- Run SWE-Rebench batches with `--sample`, `--skip`, `--instance-ids`,
  `--repo`, and `--export`.

## More

- Architecture: [docs/architecture.md](docs/architecture.md)
- OpenClaw guide: [docs/operator-guide.md](docs/operator-guide.md)
- Sidecar reference: [docs/sidecar.md](docs/sidecar.md)
- Trace & protocol: [docs/trace-schema-v6.md](docs/trace-schema-v6.md)
- Deployment: [docs/deployment.md](docs/deployment.md)
- ARM/QEMU setup: [docs/arm-qemu.md](docs/arm-qemu.md)
- SWE-Rebench guide: [swe_rebench/README.md](swe_rebench/README.md)

### Troubleshooting

| Problem area | See |
|---|---|
| OpenClaw / plugin / sidecar errors | [operator-guide.md#troubleshooting](docs/operator-guide.md#troubleshooting) |
| Cgroup permission errors | [Cgroup Troubleshooting](#cgroup-troubleshooting) above |
| SWE-Rebench failures (Docker, timeout, eBPF) | [swe_rebench/README.md#troubleshooting](swe_rebench/README.md#troubleshooting) |
| ARM/Kunpeng QEMU issues | [arm-qemu.md#kunpeng-troubleshooting](docs/arm-qemu.md#kunpeng-troubleshooting) |
