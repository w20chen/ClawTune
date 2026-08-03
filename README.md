# ClawTune

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%E2%89%A52026.7.1-6e40c9.svg)](https://openclaw.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

ClawTune adds hardware-aware tracing and scheduling to OpenClaw. It combines
an OpenClaw plugin with a local scheduler sidecar and uses eBPF to measure the
CPU, memory, process lifecycle, model calls, and tool calls of real agent work.
It can also run SWE-Rebench tasks and export the resulting traces. eBPF collection is enabled and required by default.

## Supported Hosts

| Host | Status | Notes |
| --- | --- | --- |
| Kunpeng / arm64 + openEuler | Supported | eBPF runs natively; the setup command enables QEMU for amd64 benchmark images |
| x86_64 Linux | Supported | eBPF and benchmark images run natively |

The Linux host needs Docker, Node.js/npm, OpenClaw 2026.7.1 or newer, Python
3.10 or newer, Linux 5.8 or newer, cgroup v2, and development files matching
the running kernel. The setup command installs the BCC/Clang/kernel packages
it can safely identify; it reports Docker, Node.js, or OpenClaw as one
consolidated missing-software list instead of attempting to replace an
existing installation.

## Quick Start

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
- installs, builds, and configures the OpenClaw plugin;
- repairs a stale ClawTune plugin link after the repository has moved;
- configures automatic privileged sidecar startup with a readiness gate;
- builds and validates the parser adapter for the privileged host runtime;
- enables and tests amd64 Docker images automatically on Kunpeng;
- compiles, attaches, and exercises the real eBPF collector.

A successful run prints `Setup and eBPF validation passed; the validation
process has exited.` This means the temporary validation process finished; the
plugin starts the real sidecar when OpenClaw needs it. You can rerun setup after
an update because it reuses healthy state. To see every detected path in one
report, run:

```bash
python3 scripts/clawtune.py doctor
```

### 2. Configure the model provider

`setup` creates `.env` and `swe_rebench/config.yaml` without overwriting an
existing file.

For SWE-Rebench, export the provider key in the shell that starts the run:

```bash
export LLM_API_KEY="<provider-api-key>"
```

The benchmark wrapper preserves only an explicit allow-list through `sudo`, so
the key reaches the runner without `sudo -E` and without being copied into a
command-line argument. As a persistent alternative, put the key on one line in
the ignored file `swe_rebench/llm_api_key.txt`. Then edit the model values in
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

Run OpenClaw normally. The configured plugin starts the privileged eBPF
sidecar and waits for readiness before the first model request. Replace the
model name with the one you configured:

```bash
openclaw agent --local --agent main \
  --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'." \
  --session-key "<set a session key>"
# output "clawtune-ok"
```

If a sidecar is already running, the plugin reuses it. The explicit
`python3 scripts/clawtune.py agent ...` wrapper remains available for
non-interactive environments where plugin-spawned sudo cannot use a terminal.
The plugin resolves the current checkout, `.venv`, matching kernel build tree,
and privileged launch arguments at runtime. It does not persist a generated
absolute sidecar command that would become stale after the checkout moves.

Traces are written under `data/traces/`. A healthy API alone is not used as
proof of eBPF readiness; `setup` and `check` both execute a real instrumented
command.

### 4. Run a benchmark

If the SWE-Rebench task dataset is in the usual sibling
`../agent-test-bench` checkout, ClawTune finds it automatically:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

Note that this command uses the `host-openclaw-sandbox` mode by default. For `container-openclaw`, set `--runtime-mode container-openclaw`.

You may instead provide a dataset explicitly:

```bash
python3 scripts/clawtune.py benchmark \
  --dataset /path/to/tasks.json --sample 1
```

On Kunpeng, the wrapper defaults benchmark containers to `linux/amd64`; on
x86_64 it uses the native platform. An explicit
`SWE_REBENCH_DOCKER_PLATFORM` environment value always wins. ClawTune passes
the selected platform to Docker without adding unsupported keys to OpenClaw's
configuration. Results are kept in `swe_rebench/.runtime/`.

Start serially to verify credentials, Docker, OpenClaw, and eBPF together:

```bash
python3 scripts/clawtune.py benchmark --sample 1 --parallelism 1
```

Then increase task concurrency explicitly. One benchmark invocation owns one
machine-wide Sidecar; all concurrent OpenClaw runtimes reuse it and contribute
to the same batch knowledge base:

```bash
# Small acceptance run
python3 scripts/clawtune.py benchmark --sample 8 --parallelism 4

# Large-host example; size this to the actual machine
python3 scripts/clawtune.py benchmark --sample 128 --parallelism 128
```

`--sample` selects how many cases run; `--parallelism` limits simultaneous
cases. Parallelism defaults to `1`, so an upgrade never starts a large batch
implicitly. CPU capacity is derived from online CPUs, affinity, and cgroup
limits; `128` is an example, not a hardcoded limit. On a 320-core host, a
useful future Gateway layout is 8 Gateways with up to 16 sessions each. The
current benchmark may use independent OpenClaw runtimes with the same
Plugin-to-Sidecar protocol.

## Documentation

- [Complete installation and first run](docs/getting-started.md)
- [Configuration](docs/configuration.md)
- [Kunpeng and arm64](docs/arm-qemu.md)
- [Troubleshooting](docs/troubleshooting.md)
- [SWE-Rebench usage](swe_rebench/README.md)
- [Architecture and developer references](docs/architecture.md)

## Development Checks

```bash
python tools/validate_contracts.py
python -m pytest tests -q
python -m pytest services/scheduler/tests -q
cd packages/openclaw-plugin && npm test && npm run typecheck
```

The JSON Schemas in `contracts/` are the public protocol source of truth.
Placement recommendations remain advisory in the current release.
