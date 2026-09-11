# ClawTune

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%E2%89%A52026.7.1-6e40c9.svg)](https://openclaw.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

ClawTune adds hardware-aware tracing and profiling to OpenClaw. The OpenClaw
plugin sends lifecycle events to a local sidecar, which records model/tool
traces and learns duration, CPU, memory, and PMU predictions from valid
telemetry. Placement advice is advisory in this MVP.

There are three supported workflows:

| Workflow | Entry point | State |
| --- | --- | --- |
| Daily OpenClaw use | `openclaw gateway run` | Persistent user KB |
| Online benchmark simulation | `python3 scripts/clawtune.py benchmark ...` | New run-owned, learning KB |
| Frozen trace evaluation | `python3 scripts/clawtune.py offline ...` | Task-held-out, read-only test KB |

The supported hosts are Kunpeng/arm64 openEuler and x86_64 Linux. They need
Docker, Node.js/npm, OpenClaw 2026.7.1 or newer, Python 3.10 or newer, Linux
5.8 or newer, cgroup v2, and development files matching the running kernel.

## Quick Start

Run commands as a normal user from the repository root. Setup elevates only
the package, QEMU, ownership-repair, and eBPF operations that need it.

### 1. Prepare the host

```bash
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py doctor
```

Setup creates `.env` and `configs/benchmark.yaml` without overwriting existing
files, installs/builds the plugin and sidecar, and exercises the real eBPF
collector. A valid collector check ends with:

```text
[ClawTune] Setup and eBPF validation passed; the validation process has exited.
```

If it does not, correct the reported host issue and run
`python3 scripts/clawtune.py check` before accepting a trace as valid.

### 2. Configure the provider

For normal OpenClaw use, point an OpenAI-compatible provider at ClawTune's
local proxy:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
```

For benchmarks, export the key and edit the model values in
`configs/benchmark.yaml`:

```bash
export LLM_API_KEY="<provider-api-key>"
```

```yaml
llm:
  upstream_base_url: "https://api.deepseek.com"
  model: "your-model-name"
  openclaw_model_ref: "vllm/your-model-name"
```

The ignored `configs/llm_api_key.txt` is the persistent alternative. Never
commit `.env`, provider credentials, benchmark workspaces, or raw traces.

### 3. Run OpenClaw

For an ongoing conversation, run one Gateway and attach a TUI:

```bash
# terminal 1
openclaw gateway run

# terminal 2
openclaw tui --session main
```

The Gateway owns sessions and runs; the plugin keeps one compatible sidecar
available and finalizes trace state after each turn. Docker is the tool
execution boundary, not another conversation owner.

For a one-shot installation smoke test:

```bash
openclaw agent --local --agent main \
  --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'."
```

Use `python3 scripts/clawtune.py sidecar` only when a service manager or a
non-interactive environment must own the privileged sidecar explicitly.
Traces are written below `traces/`; daily KB state defaults to
`~/.local/state/clawtune/kb` and can be moved with `CLAWTUNE_STATE_DIR`.

### 4. Simulate users with a benchmark

```bash
python3 scripts/clawtune.py benchmark --list
python3 scripts/clawtune.py benchmark --sample 2 --dry-run
python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 2
python3 scripts/clawtune.py benchmark --benchmark deep-research-bench --dataset /data/research.jsonl --sample 2
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified --dataset /data/verified.jsonl --sample 2
python3 scripts/clawtune.py benchmark --benchmark bfcl --category multi_turn_base --sample 2
python3 scripts/clawtune.py benchmark --benchmark terminal-bench --dataset /data/terminal-bench/tasks --sample 2
```

`--sample N` takes the first N tasks after filtering; it is not random.
`--parallelism N` bounds the number of tasks in flight (`1` is serial). Tasks
do not wait for one another: accepted observations update the shared in-memory
predictor and are coalesced by one asynchronous KB writer. Per-task cleanup
waits only for that runtime's finalizers; one final durability barrier commits
all queued updates before the run completes. Every invocation starts an
independent KB from the immutable seed.
Outputs live in `.runtime/benchmarks/<benchmark>/<run>/`; resume is allowed
only at a fully saved task boundary. These runs measure prediction and
learning, not official task solve scores (`official_score` is `null`).

See [benchmark adapters and input formats](docs/MULTI_BENCHMARK_IMPLEMENTATION.md).

### 5. Evaluate fixed traces

```bash
python3 scripts/clawtune.py offline --dataset /data/fixed-traces --rss-unit MiB
# Legacy traces without benchmark metadata need an explicit identity:
python3 scripts/clawtune.py offline --dataset /data/swe-traces \
  --benchmark swe-rebench --rss-unit MiB
python3 scripts/clawtune.py kb status
```

Offline evaluation keeps complete tasks and attempts together, creates or
reuses a deterministic per-benchmark/per-group split under
`.runtime/offline/splits/`, trains one seed per dataset, and never updates it
while testing. Outputs contain `split.json`, `seed/`, `predictions.jsonl`,
`report.json`, and `report.md`. Missing CPU, memory, or PMU labels remain
unavailable rather than being filled with zero.

## Documentation

Start with the [documentation map](docs/README.md). The main operational guides
are [installation](docs/getting-started.md),
[configuration](docs/configuration.md),
[sidecar reference](docs/sidecar.md),
[trace and protocol reference](docs/trace-schema.md), and
[troubleshooting](docs/troubleshooting.md).

## Development Checks

```bash
python tools/validate_contracts.py
python -m pytest tests -q
python -m pytest services/sidecar/tests -q
cd packages/clawtune-plugin && npm test && npm run typecheck
```

The JSON Schemas in `contracts/` are the public protocol source of truth.
