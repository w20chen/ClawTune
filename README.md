# ClawTune

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%E2%89%A52026.7.1-6e40c9.svg)](https://openclaw.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

ClawTune adds hardware-aware tracing and profiling to OpenClaw. It combines
an OpenClaw plugin with a local sidecar and uses eBPF to measure the
CPU, memory, process lifecycle, model calls, and tool calls of real agent work.
It can also run agent benchmarks (SWE-Rebench and Deep Research Bench) and export the resulting traces. eBPF collection is enabled and required by default.

## Supported Hosts

| Host | Status | Notes |
| --- | --- | --- |
| Kunpeng / arm64 + openEuler | Supported | eBPF runs natively; the setup command enables QEMU for amd64 benchmark images of SWE-Rebench |
| x86_64 Linux | Supported | eBPF and benchmark images run natively |

The Linux host needs Docker, Node.js/npm, OpenClaw 2026.7.1 or newer, Python
3.10 or newer, Linux 5.8 or newer, cgroup v2, and development files matching
the running kernel. The setup command installs the BCC/Clang/kernel packages
it can safely identify; it reports Docker, Node.js, or OpenClaw as one
consolidated missing-software list instead of attempting to replace an
existing installation.

## Quick Start

Run these commands as a user from the repository root. An active Conda
environment is harmless: the setup program deliberately selects the system
Python that owns the distribution's `bcc` or `bpfcc` binding.

### 1. Prepare the host

```bash
python3 scripts/clawtune.py setup
```

A successful collector check prints `Setup and eBPF validation passed; the
validation process has exited.` If the collector check fails, setup completes
but reports that resource attribution is unavailable; fix the host and run
`python3 scripts/clawtune.py check` before treating a trace as valid. The plugin
starts the real sidecar when OpenClaw needs it. You can rerun setup after an
update because it reuses healthy state. To inspect detected paths, run:

```bash
python3 scripts/clawtune.py doctor
```

### 2. Configure the model provider

`setup` creates `.env` and `swe_rebench/config.yaml` without overwriting an
existing file.

For normal OpenClaw use, configure an OpenAI-compatible provider that points
to ClawTune's local proxy:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
```

For SWE-Rebench, export the provider key in the shell that starts the run:

```bash
export LLM_API_KEY="<provider-api-key>"
```

The benchmark wrapper preserves only an explicit allow-list through `sudo`, so
the key reaches the runner without `sudo -E` and without being copied into a
command-line argument. As a persistent alternative, put the key on one line in
the ignored file `swe_rebench/llm_api_key.txt` (or
`deep_research_bench/llm_api_key.txt` for Deep Research Bench). Then edit the
model values in `swe_rebench/config.yaml` (or
`deep_research_bench/config.yaml` for Deep Research Bench):

```yaml
llm:
  upstream_base_url: "https://api.deepseek.com"
  model: "your-model-name"
  openclaw_model_ref: "vllm/your-model-name"
```

Secrets are ignored by Git. Do not commit `.env`, OpenClaw credentials, or
the `llm_api_key.txt` files.

### 3. Run ClawTune with OpenClaw

For normal interactive CLI use, run one local Gateway and connect the TUI from
a second terminal:

```bash
# terminal 1: owns agents, sessions, runs, hooks, and the ClawTune plugin
openclaw gateway run

# terminal 2: reuse the default session, or choose another session key
openclaw tui --session main
```

For a single‑user production environment, the recommended setup comprises one Gateway and a small number of active sessions. A session contains repeated turns. ClawTune keeps the sidecar alive with the Gateway, finalizes trace
state after each turn, and releases session fallback state when a session ends.
Docker is an execution/isolation boundary for sandboxed tools and benchmark
tasks; it is not another conversation owner and does not imply one container
per Gateway turn.

Use the following forms for narrower cases:

| Need | Command | Lifetime |
| --- | --- | --- |
| Interactive use through the Gateway | `openclaw tui --session main` | Reuses Gateway-owned sessions and sidecar |
| Interactive local use without a Gateway | `openclaw chat` | One embedded TUI process |
| One non-interactive smoke turn | `openclaw agent --local ...` | One embedded run, then process cleanup |
| Non-interactive sudo fallback | `python3 scripts/clawtune.py agent --local ...` | Wrapper starts and stops the sidecar for that invocation |

The configured plugin starts the privileged eBPF sidecar and waits for
readiness before the first model request. For example, this one-shot command
is useful as an installation smoke test:

```bash
openclaw agent --local --agent main \
  --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'." \
  --session-key "<set a session key>"
# output "clawtune-ok"
```

If a sidecar is already running, the plugin reuses it. The explicit
`python3 scripts/clawtune.py agent ...` wrapper remains available for
one-shot, non-interactive environments where plugin-spawned sudo cannot use a
terminal. It is not the normal entry point for an ongoing CLI conversation.
The plugin resolves the current checkout, `.venv`, matching kernel build tree,
and privileged launch arguments at runtime. It does not persist a generated
absolute sidecar command that would become stale after the checkout moves.
Traces are written under `traces/`.

### 4. Run a benchmark

#### SWE-Rebench

The default runtime is `host-openclaw-sandbox`; use `--runtime-mode
container-openclaw` only when OpenClaw itself must run inside the task
container. Start serially:

```bash
python3 scripts/clawtune.py benchmark --sample 1 --parallelism 1
```

On Kunpeng, the wrapper defaults benchmark containers to `linux/amd64`; on
x86_64 it uses the native platform. An explicit
`SWE_REBENCH_DOCKER_PLATFORM` environment value always wins. ClawTune passes
the selected platform to Docker without adding unsupported keys to OpenClaw's
configuration. Results are kept in `swe_rebench/.runtime/`.

Use `--dataset /path/to/tasks.json` to override the configured task source.
After the first case passes, increase concurrency explicitly. One benchmark
invocation owns one batch-local Sidecar; all concurrent OpenClaw runtimes reuse it and contribute
to the same batch knowledge base:

```bash
python3 scripts/clawtune.py benchmark --sample 8 --parallelism 4
```

`--sample` selects how many cases run; `--parallelism` limits simultaneous
cases. Parallelism defaults to `1`, so an upgrade never starts a large batch
implicitly.

#### Deep Research Bench

Deep Research Bench runs research questions through OpenClaw while
ClawTune records the same model/tool/resource telemetry. There is no per-task
image: the agent's tools run in a very basic Docker sandbox
(`python:3.11-slim` by default). Each task's trace, prompt, manifest, and
the record-only reference answer are kept under `deep_research_bench/.runtime/`.

```bash
# One smoke task from the bundled deep_research_bench/tasks.json
python3 scripts/clawtune.py drb --sample 1 --parallelism 1

# A real task source downloaded from HuggingFace
python3 -m deep_research_bench.discover --sample 32 --out deep_research_bench/tasks-32.json
python3 scripts/clawtune.py drb --dataset deep_research_bench/tasks-32.json --sample 32
```

See [Deep Research Bench usage](deep_research_bench/README.md).

## Documentation

- [Complete installation and first run](docs/getting-started.md)
- [Configuration](docs/configuration.md)
- [Kunpeng and arm64](docs/arm-qemu.md)
- [Troubleshooting](docs/troubleshooting.md)
- [SWE-Rebench usage](swe_rebench/README.md)
- [SWE-Rebench trace replay](swe_rebench/README.md#replay-a-case)
- [Deep Research Bench usage](deep_research_bench/README.md)
- [Legacy offline evaluation](docs/legacy-eval.md)
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
