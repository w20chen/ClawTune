# ClawTune

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%E2%89%A52026.7.1-6e40c9.svg)](https://openclaw.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

ClawTune adds hardware-aware tracing and profiling to OpenClaw. It combines
an OpenClaw plugin with a local sidecar and uses eBPF to measure the
CPU, memory, process lifecycle, model calls, and tool calls of real agent work.
The demo has three paths: daily OpenClaw learning, online benchmark simulation, and frozen offline trace evaluation. Five peer benchmark adapters share one run lifecycle; resource labels are accepted only when their attribution is valid.

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

`setup` creates `.env` and `configs/benchmark.yaml` without overwriting existing files. Older SWE/DRB configs remain readable through `--config`.

For normal OpenClaw use, configure an OpenAI-compatible provider that points
to ClawTune's local proxy:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
```

For all benchmarks, export the provider key in the shell that starts the run:

```bash
export LLM_API_KEY="<provider-api-key>"
```

The wrapper preserves the key through its explicit sudo allow-list. Alternatively,
use the ignored `configs/llm_api_key.txt` file. Edit `configs/benchmark.yaml`:

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

`--sample N` selects the first N tasks after filtering. It is **serial online
learning**, with one run-owned sidecar and KB. Each task gets a new workspace
and conversation, while later tasks see earlier tasks' completed observations.
A new invocation starts a new run from the immutable seed, independent of daily
state and other runs. Only `--parallelism 1` is supported.

Outputs live in `.runtime/benchmarks/<benchmark>/<run>/`. `run.json` records
selection order, per-task KB generations, errors and learning status.
`--resume /path/to/run` resumes only at a saved task boundary with the original
config and seed. An interrupted active task is rejected because it may have
partially updated the KB. `--seed /path/to/offline/seed` selects another seed.
`drb` remains a compatibility alias for the same benchmark command.

BFCL needs its native dependencies and `BFCL_REPO_PATH` pointing to a Gorilla
checkout; executable stateful categories retain native functions and state
across turns. AST-only cases are rejected explicitly. Terminal Bench copies the
native task Compose environment to the run directory and exposes `terminal_exec`
inside its `client` container. This demo measures learning and prediction;
`official_score` is null, not a task-solving leaderboard score. See the
[implementation and input formats](docs/MULTI_BENCHMARK_IMPLEMENTATION.md).

### 5. Train and evaluate fixed traces

```bash
python3 scripts/clawtune.py offline --dataset /data/fixed-traces --rss-unit MiB
# Legacy SWE traces without benchmark metadata:
python3 scripts/clawtune.py offline --dataset /data/swe-traces --benchmark swe-rebench --rss-unit MiB
python3 scripts/clawtune.py kb status
python3 scripts/clawtune.py kb status --path /path/to/run/kb
```

The offline path supports task-scoped trace v5 and v6. It groups by dataset and
repository (category for non-repository tasks), keeps all attempts/turns of a
task together, and deterministically assigns about 80% to training. Singleton
groups go to training; groups of two or more keep test tasks. Each dataset
trains its **own** three-layer seed; tests never update it. Outputs contain
`split.json`, `seed/`, `predictions.jsonl`, and `report.json` / `report.md`.
Missing CPU/memory labels are unavailable, never zero-filled.

Daily KB state defaults to `~/.local/state/clawtune/kb`; set `CLAWTUNE_STATE_DIR`
to relocate it. Trace export directories do not select a KB. Three snapshots
commit together through `CURRENT`; a single writer lock prevents simultaneous
writers, and restarts restore the last committed generation. Uncommitted
observations from an abrupt termination can be lost. Seeds are never writable.

## Documentation

- [Complete installation and first run](docs/getting-started.md)
- [Configuration](docs/configuration.md)
- [ClawTune Sidecar reference](docs/sidecar.md)
- [Kunpeng and arm64](docs/arm-qemu.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Trace & protocol reference](docs/trace-schema.md)
- [SWE-Rebench usage](swe_rebench/README.md)
- [SWE-Rebench trace replay](swe_rebench/README.md#replay-a-case)
- [Deep Research Bench usage](deep_research_bench/README.md)
- [Offline dataset evaluation](docs/legacy-eval.md)
- [Evaluation report](docs/legacy_eval_final_report.md)
- [Architecture and developer references](docs/architecture.md)

## Development Checks

```bash
python tools/validate_contracts.py
python -m pytest tests -q
python -m pytest services/sidecar/tests -q
cd packages/clawtune-plugin && npm test && npm run typecheck
```

The JSON Schemas in `contracts/` are the public protocol source of truth.
Placement recommendations remain advisory in the current release.
