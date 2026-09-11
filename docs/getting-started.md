# Installation and first run

Run shell examples from the repository root on Linux. Commands containing
`<...>` require your own value. For Windows development use the commands in
[the root README](../README.md#development); live benchmarks require Linux.

## Prerequisites

Use Python 3.10+ and a normal login user with sudo access. Install Docker,
Node.js/npm and OpenClaw 2026.7.1+ through your host's supported installation
method; ClawTune does not install these applications. Check:

```bash
docker info
node --version
npm --version
openclaw --version
```

The host needs Linux 5.8+, cgroup v2 and development headers matching the
running kernel. Setup locates the system Python with BCC bindings, installs
identifiable BCC/Clang/kernel packages using apt or dnf, and enables amd64
containers on arm64. See [ARM/QEMU](arm-qemu.md) for that platform.

## Setup and collector verification

```bash
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py doctor
```

Do not sudo the whole setup command. Setup elevates the necessary operations,
creates `.venv`, installs/builds the sidecar and plugin, configures the trusted
launcher, and creates `.env` and `configs/benchmark.yaml` if absent.
A successful collector check prints:

```text
[ClawTune] Setup and eBPF validation passed; the validation process has exited.
```

This process is the temporary check, not the runtime sidecar. Installation can
finish after a collector failure; that does not validate strict measurements.
Correct the reported issue and run `python3 scripts/clawtune.py check`.

`setup --help` lists opt-outs for system-package installation, QEMU and the
collector check. Skipping a check does not make the corresponding runtime
requirement optional. Rerun setup after updating or moving the checkout; it
preserves configuration and refreshes installation paths.

## Choose a workflow

### Daily OpenClaw use

Set `CLAWTUNE_LLM_UPSTREAM_BASE_URL` in `.env` if using a provider other than
DeepSeek. Configure OpenClaw to send model requests through ClawTune:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
```

The proxy forwards the provider credential unless an upstream key override is
configured. If you enable `CLAWTUNE_TOKEN`, export the same token to OpenClaw
and the sidecar. It is a separate local authentication token.

```bash
# terminal 1
openclaw gateway run
# terminal 2
openclaw tui --session main
```

Setup configures the plugin to start/reuse a compatible sidecar. For a one-shot
smoke turn:

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run uname -a and summarize it."
```

CLI syntax can vary across OpenClaw releases; use its installed `agent --help`
if it rejects the agent selector. The benchmark runner probes this difference.

For a service manager, or when automatic privileged startup cannot prompt,
start `python3 scripts/clawtune.py sidecar` explicitly and then launch OpenClaw.
The `python3 scripts/clawtune.py agent ...` wrapper owns a temporary sidecar
when needed and forwards its remaining arguments to OpenClaw's agent command.

Inspect one resulting trace and the daily KB:

```bash
python tools/inspect_trace.py traces/<file>.jsonl --all --details
python3 scripts/clawtune.py kb status
```

A healthy HTTP endpoint alone does not establish collector or prediction
quality. See [trace interpretation](trace-schema.md) and [troubleshooting](troubleshooting.md).

### Online benchmark

Set model and provider values in `configs/benchmark.yaml`, and supply
`LLM_API_KEY` or the raw key in `configs/llm_api_key.txt`. Then follow the
[benchmark guide](benchmarks.md) for your dataset. Benchmark provider settings
are independent of the daily OpenClaw provider configuration.

### Fixed-trace evaluation

No model provider or running Docker/sidecar is needed. Use the
[offline guide](offline.md) with an existing trace directory and its RSS unit.
