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

For model proxy setup, smoke testing, and troubleshooting, see
[docs/operator-guide.md](docs/operator-guide.md). Quick start:

```bash
# 1. Start sidecar
cp .env.example .env
python3 -m agent_scheduler.main --host 127.0.0.1 --port 8765 &

# 2. Configure OpenClaw to route through sidecar proxy
#    Set provider base URL to http://127.0.0.1:8765/v1, model to <your-model>
#    Any OpenAI-compatible provider works. Examples:

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
openclaw agent --local --agent main --model "vllm/<your-model>" \
  --message "Use the shell to run: python -c 'print(\"trace-ok\")'. Then summarize the result." \
  --session-key "my-session"
```

The sidecar must be running before the plugin hooks fire and before the first
model request. See [operator-guide.md](docs/operator-guide.md) for model proxy
configuration (vLLM onboarding, OpenRouter, API key setup) and smoke test
verification.

`enableCgroup: true` (the default) enables per-tool cgroup-v2 resource
attribution. Most environments work out of the box. If you encounter
`cgroup_join_failed ... Permission denied`, see
[operator-guide.md#troubleshooting](docs/operator-guide.md#troubleshooting).

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
| Cgroup permission errors | [operator-guide.md#troubleshooting](docs/operator-guide.md#troubleshooting) |
| SWE-Rebench failures (Docker, timeout, eBPF) | [swe_rebench/README.md#troubleshooting](swe_rebench/README.md#troubleshooting) |
| ARM/Kunpeng QEMU issues | [arm-qemu.md#kunpeng-troubleshooting](docs/arm-qemu.md#kunpeng-troubleshooting) |
