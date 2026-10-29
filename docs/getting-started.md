# Installation and Use

Commands below run in Bash on Linux, from the repository root unless stated otherwise. Replace `<...>` with actual values.

## Library and offline use

Install the Python library without setting up OpenClaw or live collectors:

```bash
python -m pip install ./services/sidecar
```

The package includes the prediction modules, JSON Schemas, and the default
initialization bundle. For example, locate and validate the bundled knowledge base:

```python
from clawtune_kb import default_seed_directory, validate_seed

seed = default_seed_directory()
manifest = validate_seed(seed)
print(seed)
print(manifest["provenance"]["counts"])
```

Offline trace evaluation needs Python dependencies and existing traces, without
OpenClaw, a model API key, or running collectors. See the
[offline evaluation guide](benchmarks.md#offline-trace-evaluation).
The following sections cover live monitoring and OpenClaw integration.

## 1. Prepare a new machine

Install Python 3.10+, Git, Docker Engine, Node.js/npm, and OpenClaw. Use a normal account with sudo access. The host needs cgroup v2 and development headers matching the running kernel; Linux 5.8+ is the intended baseline. Deployment targets include x86_64 Linux and arm64 openEuler.

For example, install basic tools on Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y git curl python3 python3-venv python3-pip
```

On openEuler, use the corresponding distribution packages through dnf. Install [Docker Engine](https://docs.docker.com/engine/install/) for the host distribution; Setup installs the Compose and Buildx CLI plugins. The [OpenClaw installer](https://docs.openclaw.ai/install) can provision Node.js and skip onboarding:

```bash
curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard
```

The project's existing integration targets the OpenClaw 2026.7.1 interface. The installer may supply a newer version; use its local help and the checks below to establish compatibility.

Verify prerequisites:

```bash
python3 --version
node --version
npm --version
openclaw --version
sudo docker info
stat -fc %T /sys/fs/cgroup
```

The last command should print `cgroup2fs`. Docker must reach a running daemon. These are host prerequisites; setup manages the project dependencies and runtime configuration. Setup needs network access to package registries and release downloads, and your account must be allowed to use sudo.

Obtain the source and install:

```bash
git clone https://github.com/w20chen/claw.git ClawTune
cd ClawTune
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py doctor
```

For an existing checkout, enter that directory instead. Do not sudo the entire setup command. It elevates individual operations, selects a system Python with BCC, creates `.venv`, installs collector dependencies, builds and enables the plugin, and creates `.env` and `configs/benchmark.yaml`. Existing configuration and credentials are preserved. Setup installs a pinned Compose/Buildx pair (backing up replaced user plugin binaries), then tests an actual image build with the same Docker configuration used after sudo.

Successful collector validation prints:

```text
[ClawTune] Setup, Docker build, and eBPF validation passed; validation processes have exited.
```

Setup returns a failure if required validation fails. Correct the reported error and rerun setup, or recheck an installed environment with:

```bash
python3 scripts/clawtune.py check
```

### ARM hosts

Setup configures QEMU/binfmt for amd64 task containers. The monitoring service remains native to the host. Benchmark startup and `check` restore a missing handler, including after a reboot. To verify container execution separately:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh check
```

On arm64, repository benchmarks default to `linux/amd64` when no platform is configured. Set `docker.platform: linux/arm64` in your benchmark YAML for native ARM images, or override it for one shell:

```bash
export SWE_REBENCH_DOCKER_PLATFORM=linux/arm64
```

This environment value overrides YAML platform configuration. Terminal tasks use their own Compose platform settings.

## 2. Daily OpenClaw operation

For benchmark-only use, skip this section and continue with the [benchmark guide](benchmarks.md).

Set the upstream model address in root `.env` when using a provider other than the default DeepSeek:

```dotenv
CLAWTUNE_LLM_UPSTREAM_BASE_URL=https://your-provider.example
# Shared by the LLM proxy and background command-rule generation.
CLAWTUNE_LLM_UPSTREAM_API_KEY=<provider-api-key>
CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL=<model>
```

Route OpenClaw model requests through the local proxy:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
openclaw config validate
```

The proxy uses `CLAWTUNE_LLM_UPSTREAM_API_KEY_OVERRIDE` first, then
`CLAWTUNE_LLM_UPSTREAM_API_KEY`, then the credential supplied by OpenClaw.
Background [command-rule generation](command-parsing.md#automatic-rule-generation)
requires one of the two sidecar key settings; an OpenClaw request header alone
cannot enable it. If your version rejects these onboarding flags, use
`openclaw onboard --help` to configure the same local endpoint, model, and provider key.

The proxy supports OpenAI-compatible chat completions, including JSON responses
and streaming responses. Set the requested model in OpenClaw. To expose a different
model name locally, set `CLAWTUNE_LLM_PROXY_EXPOSE_MODEL` to that name and
`CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL` to the provider's model name; setting only the
latter does not rewrite the model in ordinary requests. Generation can use a
separate model and URL as described in the command-rule guide.

Start two terminals:

```bash
# Terminal 1
openclaw gateway run
```

```bash
# Terminal 2
openclaw tui --session main
```

Ask the agent to execute a shell command, such as “Run `uname -a` and explain the output.” The plugin starts or reuses the local service. For a one-shot invocation:

```bash
python3 scripts/clawtune.py agent --local --agent main \
  --model "vllm/<model>" --message "Use the shell to run uname -a and summarize it."
```

The wrapper forwards arguments to OpenClaw; consult `openclaw agent --help` for version-specific agent selection. If a service manager must own the privileged process, start `python3 scripts/clawtune.py sidecar` explicitly before OpenClaw.

The sidecar prepares its eBPF collectors before reporting ready. A fresh process
can take several seconds to start; subsequent tool calls reuse those collectors.
Keep the service running across calls to avoid paying this startup cost again.

Plugin configuration is validated against `openclaw.plugin.json`, including
nested trace settings. `profilingMode` supports only `off`: this disables an
additional external profiler, not execution eBPF collection or configured PMU
counters. `proc`, `perf`, `ksys`, and `vtune` are rejected because their external
profiler backends are not implemented. Configure the existing PMU collector
with the sidecar PMU settings instead. `enableNuma: false` prevents explicit
memory-node placement in the launcher; ordinary placement advice remains
advisory regardless of this switch.

## 3. Output and persistent state

Daily traces are JSONL files under `traces/`. Inspect a generated file and the saved prediction state:

```bash
.venv/bin/python tools/inspect_trace.py traces/<file>.jsonl --all --details
python3 scripts/clawtune.py kb status
```

Expect model and tool events, pre-execution predictions, and post-execution measurements. KB training retains approximate native-tool measurements, fills short/insufficient-sampling targets with zero, and retains partial timeout observations; raw quality and interruption markers remain visible. Missing historical evidence can still produce unavailable predictions. Use the [trace measurement and prediction quick guide](tool-profile.md#quick-guide-measurements-and-predictions) to distinguish measurements from training labels; the prediction method is described in the [technical report](technical-report.md).

| Workflow | State location and lifetime |
| --- | --- |
| Daily operation | `~/.local/state/clawtune/kb/` by default, respecting `XDG_STATE_HOME`; persists across restarts |
| Online benchmark | A separate `<run>/kb/`, shared by tasks within that run |
| Offline evaluation | Experiment-owned `seed/`, constructed from training data and frozen for testing |

Online updates become available before they are saved to disk. Normal service
shutdown and benchmark completion save the knowledge base; a forced exit can
lose changes since the last save. Large knowledge bases can delay shutdown.
Unreadable saved state stops startup rather than silently starting empty.

### Default knowledge base and custom initialization

New daily state and online runs copy `seeds/bootstrap-v2` by default. This
initial knowledge base contains 626 tool calls from 14 trace-v6 tasks across
12 repositories, including the former test tasks. Those tasks can no longer
measure independent test accuracy for this bundle.

All supported training targets have numeric values, but some are approximate,
partial, or filled with zero because no measurement was available. This applies
to CPU, memory, and hardware counters as well as duration. The
[manifest](../seeds/bootstrap-v2/manifest.json) records sources and target counts;
the [measurement guide](tool-profile.md#quick-guide-measurements-and-predictions)
explains how to distinguish training values from measurements.

To export all tasks from a recorded repository split into a new deployment bundle:

```bash
python3 scripts/build_replayed_seed.py --dataset <trace-directory> \
  --split <split.json> --include-test --output <new-seed-directory>
```

The output directory must be new. Omit `--include-test` to export only the training
partition for held-out evaluation. Input traces are not modified.
Select a different initialization bundle with
`CLAWTUNE_KB_SEED` for daily use or `--seed <directory>` for benchmarks.
Changing the prior does not reset existing state. Resuming an old run requires
its original prior. The offline command's `--seed` is instead an integer split
seed.

### Rebuilding and compatibility

`seeds/bootstrap-v1` remains available as a historical replay baseline.
It is separate from the current default bundle; consult each manifest before
comparing data sources or measurement coverage.

To rebuild the causal seed from fresh trace-v6 replays on Linux, write to a new directory and repeat `--trace` once per task:

```bash
python3 scripts/build_replayed_seed.py \
  --trace owner__repo-1=/path/to/replay-1/trace.jsonl \
  --trace owner__repo-2=/path/to/replay-2/trace.jsonl \
  --output /path/to/new-seed
```

For legacy trace-v5 data, use:

```bash
python3 scripts/build_bootstrap_seed.py --dataset /path/to/traces \
  --cases 10 --rss-unit MiB --output /path/to/new-seed
```

Set `--rss-unit` to the trace-v5 source measurements' unit. Both builders record source hashes and available targets in the manifest. Point `CLAWTUNE_KB_SEED` or benchmark `--seed` at the new directory to use it for new state.

For incompatible saved formats, rebuild from original traces into a new bundle
and start a new benchmark run or `CLAWTUNE_STATE_DIR`. New writable state can
initialize incompatible feature-based components empty; it does not recover
their prior learning. Frozen loading requires compatible snapshots. Changing the
initialization directory does not migrate existing state.

### Common settings

Common settings belong in root `.env`; restart the service after changes:

| Setting | Purpose |
| --- | --- |
| `CLAWTUNE_TRACE_DIR` | Daily trace output |
| `CLAWTUNE_STATE_DIR` | Daily state root; a new directory starts an independent state |
| `CLAWTUNE_TOKEN` | Optional local API token; export the same value to OpenClaw and the service, separately from the provider key |
| `CLAWTUNE_PMU_ENABLED` | Enable best-effort hardware counting |
| `CLAWTUNE_TOOL_RESOURCE_FROZEN` | Freeze daily learning |
| `CLAWTUNE_KB_MAX_OPTIONAL_FEATURES` | Fixed optional-feature combination limit for Lattice (default 6; nonnegative integer) |
| `CLAWTUNE_TOOL_RESOURCE_MEMORY_MEASUREMENT` | Query memory evidence from `cgroup_v2_environment_union_v1` (default), `cgroup_v2_memory_current`, or `guest_memtotal_minus_memavailable`; selects a source without converting labels |

Lattice retains complete command feature sets. The optional-feature
limit bounds the size of partial combinations used for matching; it is not a
total-node or memory limit. Larger values can increase construction time and
memory. The saved model and the service must use the same setting. To change
it, rebuild from raw traces with offline `--kb-max-optional-features <n>` and
start new state with the matching environment setting.

The complete settings are maintained in [.env.example](../.env.example) and the [plugin configuration schema](../packages/clawtune-plugin/openclaw.plugin.json). Existing environment variables take precedence over `.env`. The service binds to loopback and is not configured for public exposure.

## 4. Troubleshooting

| Symptom | Action |
| --- | --- |
| Missing BCC/headers or eBPF compilation failure | Check `uname -r` and `/lib/modules/$(uname -r)/build`; rerun setup with its selected system Python |
| Failure after moving or updating the checkout | Rerun setup from the current path |
| Connection refused on 8765, or sudo cannot prompt | Start the local service explicitly to expose errors; run `sudo -v` first if needed |
| No model events | Check the local `/v1` proxy address, upstream URL, model, and credential |
| Conversation hook rejected | Check `openclaw config get plugins.entries.clawtune.hooks`; rerun setup and restart OpenClaw |
| Image fails on ARM | Run the QEMU check above and verify image architecture |
| PMU unavailable | Check event support, permissions, and multiplexing; zero-filled training values do not confirm successful counting |
| Benchmark input, search, or resume failure | Use the relevant section of the [benchmark guide](benchmarks.md) |

API liveness does not validate kernel collection. Repeat the collector check after kernel, BCC, or Clang changes.

## 5. Development checks

For development or offline processing only, install Python dependencies without performing Linux deployment. Run the two Python suites in their respective package contexts:

```bash
python -m pip install -e 'services/sidecar[dev]'
python -m pytest tests -q
(cd services/sidecar && python -m pytest -q)
python tools/validate_contracts.py
python tools/validate_docs.py
(cd packages/clawtune-plugin && npm ci && npm test && npm run typecheck)
```

In PowerShell, enter the directories separately instead of using the parenthesized Bash commands. Root-wide recursive Python test discovery is unsupported.

On each target Linux architecture, also run:

```bash
.venv/bin/python tools/validate_pmu.py --require-reliable \
  --concurrency 8 --max-active 8 --high-concurrency 64 \
  --benchmark-count 40 --output .runtime/validation/pmu.json
```

Use the deployment's required perf permissions. Software tests do not establish hardware measurement accuracy.
