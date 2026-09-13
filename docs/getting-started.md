# Installation and Use

Commands below run in Bash on Linux, from the repository root unless stated otherwise. Replace `<...>` with actual values. Windows supports source development and offline checks; live resource collection requires Linux.

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

The proxy forwards the provider credential supplied by OpenClaw. If your version rejects these onboarding flags, use `openclaw onboard --help` to configure the same local endpoint, model, and provider key.

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

## 3. Output and persistent state

Daily traces are JSONL files under `traces/`. Inspect a generated file and the saved prediction state:

```bash
.venv/bin/python tools/inspect_trace.py traces/<file>.jsonl --all --details
python3 scripts/clawtune.py kb status
```

Expect model and tool events, pre-execution predictions, and post-execution measurements. Missing historical evidence or ineligible measurements produce unavailable targets; definitions are in the [technical report](technical-report.md).

| Workflow | State location and lifetime |
| --- | --- |
| Daily operation | `~/.local/state/clawtune/kb/` by default, respecting `XDG_STATE_HOME`; persists across restarts |
| Online benchmark | A separate `<run>/kb/`, shared by tasks within that run |
| Offline evaluation | Experiment-owned `seed/`, constructed from training data and frozen for testing |

New daily state and online runs copy the bundled initialization prior by default. Override with `CLAWTUNE_KB_SEED` for daily use or `--seed <directory>` for benchmarks. Changing the prior does not reset existing state. Resuming an old run requires its original prior. The offline command's `--seed` is instead an integer split seed.

Resource KB snapshots use Runtime v3, Trie v6, and Lattice v3. Start a new benchmark run or set `CLAWTUNE_STATE_DIR` to a new directory after upgrading an older state; older snapshots are rejected. Memory predictions expose total environment peak and extra peak above its pre-execution baseline. A short or failed measurement leaves that target unavailable and does not fail the tool.

Common settings belong in root `.env`; restart the service after changes:

| Setting | Purpose |
| --- | --- |
| `CLAWTUNE_TRACE_DIR` | Daily trace output |
| `CLAWTUNE_STATE_DIR` | Daily state root; a new directory starts an independent state |
| `CLAWTUNE_TOKEN` | Optional local API token; export the same value to OpenClaw and the service, separately from the provider key |
| `CLAWTUNE_PMU_ENABLED` | Enable best-effort hardware counting |
| `CLAWTUNE_TOOL_RESOURCE_FROZEN` | Freeze daily learning |

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
| PMU unavailable | Inspect the reason: event support, permissions, and multiplexing affect eligibility |
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
