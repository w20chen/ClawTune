# ClawTune

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%E2%89%A52026.7.1-6e40c9.svg)](https://openclaw.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

ClawTune adds hardware-aware tracing and scheduling to OpenClaw. It combines
an OpenClaw plugin with a local Scheduler sidecar and uses eBPF to measure the
CPU, memory, process lifecycle, model calls, and tool calls of real agent work.
It can also run SWE-Rebench tasks and export the resulting traces.

eBPF collection is enabled and required by default. ClawTune stops early when
the kernel collector is not healthy instead of silently producing incomplete
results.

## Supported hosts

| Host | Status | Notes |
| --- | --- | --- |
| Kunpeng / arm64 + openEuler | Primary target | eBPF runs natively; the setup command enables QEMU for amd64 benchmark images |
| x86_64 Linux | Supported | eBPF and benchmark images run natively |
| Other Linux distributions | Expected to work | The setup command currently understands `dnf` and `apt` |
| Windows and macOS | Development only | They cannot run the Linux eBPF collector |

The Linux host needs Docker, Node.js/npm, OpenClaw 2026.7.1 or newer, Python
3.10 or newer, cgroup v2, and kernel development files matching the running
kernel. The setup command installs the BCC/Clang/kernel packages it can safely
identify; it reports Docker, Node.js, or OpenClaw as one consolidated missing-
software list instead of attempting to replace an existing installation.

## From checkout to a running system

Run these commands as a normal user from the repository root. An active Conda
environment is harmless: the setup program deliberately selects the system
Python that owns the distribution's `bcc` or `bpfcc` binding.

### 1. Prepare the host

```bash
python3 scripts/clawtune.py setup
```

This one command:

- detects openEuler/RHEL (`dnf`) or Debian/Ubuntu (`apt`);
- installs missing eBPF compiler, BCC, and matching kernel packages;
- creates one reusable `.venv` that can see the system BCC binding;
- installs the Scheduler and builds/configures the OpenClaw plugin;
- enables and tests amd64 Docker images automatically on Kunpeng;
- compiles, attaches, and exercises the real eBPF collector.

Success ends with `安装完成`. You can rerun the command after an update; it
reuses healthy state. To see every detected path in one report, run:

```bash
python3 scripts/clawtune.py doctor
```

### 2. Configure the model provider

`setup` creates `.env` and `swe_rebench/config.yaml` without overwriting an
existing file.

For SWE-Rebench, put the provider key in the ignored file
`swe_rebench/llm_api_key.txt`, then edit these two values in
`swe_rebench/config.yaml`:

```yaml
llm:
  upstream_base_url: "https://api.deepseek.com"
  model: "your-model-name"
  openclaw_model_ref: "vllm/your-model-name"
```

For normal OpenClaw use, configure an OpenAI-compatible provider that points
to ClawTune's local proxy:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
```

Secrets are ignored by Git. Do not commit `.env`, OpenClaw credentials, or
`swe_rebench/llm_api_key.txt`.

### 3. Run ClawTune with OpenClaw

Start the sidecar and keep this terminal open:

```bash
python3 scripts/clawtune.py sidecar
```

In another terminal, run an agent. Replace the model name with the one you
configured:

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'."
```

Traces are written under `data/traces/`. A healthy API alone is not used as
proof of eBPF readiness; `setup` and `check` both execute a real instrumented
command.

### 4. Run a benchmark

If the SWE-Rebench task dataset is in the usual sibling
`../agent-test-bench` checkout, ClawTune finds it automatically:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

You may instead provide a dataset explicitly:

```bash
python3 scripts/clawtune.py benchmark \
  --dataset /path/to/tasks.json --sample 1
```

On Kunpeng, the command automatically selects `linux/amd64`; on x86_64 it uses
the native platform. Results are kept in `swe_rebench/.runtime/`.

## Documentation

- [Complete installation and first run](docs/getting-started.md)
- [Configuration](docs/configuration.md)
- [Kunpeng and arm64](docs/arm-qemu.md)
- [Troubleshooting](docs/troubleshooting.md)
- [SWE-Rebench usage](swe_rebench/README.md)
- [Architecture and developer references](docs/architecture.md)

## Development checks

```bash
python tools/validate_contracts.py
python -m pytest tests -q
python -m pytest services/scheduler/tests -q
cd packages/openclaw-plugin && npm test && npm run typecheck
```

The JSON Schemas in `contracts/` are the public protocol source of truth.
Placement recommendations remain advisory in the current release.
