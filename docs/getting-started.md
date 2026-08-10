# Installation and First Run

This guide expands the short path in the root README. It is written for a
fresh Linux checkout and covers both Kunpeng/openEuler and x86_64 Linux.

## Before You Begin

Use a normal login user with `sudo` access. Confirm that Docker, Node.js/npm,
and OpenClaw are installed and usable by that user:

```bash
docker info
node --version
npm --version
openclaw --version
```

ClawTune does not install or replace these three applications because their
repository, daemon, proxy, and security settings are site-specific. Everything
kernel/eBPF-related is handled by the project setup command on systems using
`dnf` or `apt`.

Docker must use a Linux daemon. The host needs Linux 5.8 or newer with cgroup
v2, and matching development files must be available in the distribution
repository. ClawTune supports both `x86_64` and `aarch64`; Kunpeng/openEuler is
the primary bring-up target.

## Install ClawTune

From the repository root:

```bash
python3 scripts/clawtune.py setup
```

Do not prefix the whole command with `sudo`. The script elevates only package,
QEMU, ownership-repair, and eBPF validation operations. Running the complete
setup as root would create files that your normal account cannot rebuild.

The setup command intentionally ignores the active Conda interpreter when it
does not own BCC. On openEuler it can use the distribution module named
`bpfcc`; on Debian/Ubuntu it can use `bcc`. Both expose the API ClawTune needs.
No import symlink and no `PYTHONPATH` compatibility directory are required.

The reusable environment is always `.venv`. If that directory already exists
but was created from the wrong Python, setup asks you to rename or remove that
single directory rather than attempting to combine incompatible interpreters.

At the end, setup attempts a real validation: compile the complete BPF program,
attach its probes, create a test cgroup, execute a process, and verify usable
events. The report is saved at `data/ebpf-check.json`. Setup also runs
`openclaw config validate` and builds the architecture-specific parser adapter
as the same privileged identity used by the sidecar. An unsupported OpenClaw
key or missing adapter is therefore rejected before the first agent run.

A successful setup includes:

```text
[ClawTune] Setup and eBPF validation passed; the validation process has exited.
```

The process that exited is only the temporary validation. The plugin starts
the long-running sidecar on demand. An eBPF validation failure is currently
non-fatal to setup, but strict `exec` collection and benchmark runs will still
fail closed. Correct the reported host issue and run
`python3 scripts/clawtune.py check` before accepting measurements.

## Configure a Provider

### Benchmark runs

The shortest-lived option is to export the provider key in the same shell that
starts the benchmark:

```bash
export LLM_API_KEY="<provider-api-key>"
```

The wrapper passes this value through `sudo` with a narrow environment
allow-list; it does not use broad `sudo -E`. For a persistent local setup, put
the raw key on one line in the Git-ignored file
`swe_rebench/llm_api_key.txt` (or `deep_research_bench/llm_api_key.txt` for
Deep Research Bench) instead. `LLM_API_KEY_FILE` can point to another
file when a site already manages secrets that way.

Edit `swe_rebench/config.yaml` (or `deep_research_bench/config.yaml` for Deep
Research Bench) and set the upstream URL and model names. Most
users do not need to change the runtime, Docker privilege, cgroup, bundle, or
output sections. On arm64, the SWE-Rebench wrapper defaults the Docker platform
to `linux/amd64`. An explicitly exported `SWE_REBENCH_DOCKER_PLATFORM` takes
priority; x86 stays native by default. Deep Research Bench uses a multi-arch
basic sandbox image, so `drb` does not force a platform.

### Normal OpenClaw Runs

The plugin is installed and enabled by setup. It expects the sidecar at
`http://127.0.0.1:8765`. Configure the OpenClaw model provider with proxy base
URL:

```text
http://127.0.0.1:8765/v1
```

ClawTune forwards the authorization header to the upstream provider. If the
upstream URL is not DeepSeek-compatible, set
`AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL` in `.env` and restart the sidecar.

## Start and Verify

For an ongoing CLI conversation, start one Gateway and attach the TUI:

```bash
# terminal 1
openclaw gateway run

# terminal 2
openclaw tui --session main
```

The Gateway owns the agent runtime and reusable sessions. Each submitted turn
is a run; ClawTune closes that run's trace writers and clears its correlation
state when the turn ends. One Gateway with a few sessions is the intended
normal-user shape.

For a quick one-shot verification instead, run:

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run uname -a, then summarize it."
```

`--local` creates an embedded run and exits after the reply. It is appropriate
for smoke tests and scripts, but it is not the primary entry point for a
multi-turn CLI conversation. `openclaw chat` is the interactive embedded-TUI
alternative when Gateway-only features are not needed.

The plugin asks for sudo when needed, starts the eBPF sidecar with the verified
`.venv` and kernel environment, and blocks the first agent turn until port 8765
identifies itself as a compatible ClawTune sidecar. A pre-existing compatible
sidecar is reused; an unrelated service on that port is rejected.

The automatic sidecar command is deliberately not stored in OpenClaw's JSON.
At each launch the plugin finds the current checkout from its own installed
location, then resolves `.venv`, `.env`, the running kernel's build tree, and
the required `sudo` arguments. The separate `launcherPath` remains absolute:
it is the trusted managed-execution boundary used for instrumented tools, and
setup refreshes it whenever the checkout moves.

For a non-interactive service without a controlling terminal, either provide
site-managed privilege or start the sidecar explicitly before OpenClaw:

```bash
python3 scripts/clawtune.py sidecar
```

Use the consolidated environment report at any time:

```bash
python3 scripts/clawtune.py doctor
```

Repeat the kernel-level collector test after a kernel/BCC update:

```bash
python3 scripts/clawtune.py check
```

## Run SWE-Rebench

Start with one task:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

On Kunpeng, QEMU runs are slower than native x86_64, so raise
`batch.task_timeout_seconds` only when a real task reaches the default
deadline. Task selection, concurrency, and batch semantics are described in
[SWE-Rebench usage](../swe_rebench/README.md) and the root README's
[Run a benchmark](../README.md#4-run-a-benchmark) section.

## Run Deep Research Bench

Deep Research Bench runs research questions through OpenClaw while ClawTune
records the same model/tool/resource telemetry. There is no per-task image and
no `/testbed` repository; the agent's tools run in one very basic Docker
sandbox image (default `python:3.11-slim`). A bundled three-task smoke source
is used when no dataset is named:

```bash
python3 scripts/clawtune.py drb --sample 1 --parallelism 1
```

Task selection, Tavily web-search configuration, output locations, and the
relaxed telemetry gate are described in
[Deep Research Bench usage](../deep_research_bench/README.md) and the root
README's [Run a benchmark](../README.md#4-run-a-benchmark) section.

## Updating the Checkout

After pulling commits, rerun the same idempotent setup command:

```bash
python3 scripts/clawtune.py setup
```

It rebuilds the plugin, refreshes the editable Scheduler installation, retains
your existing `.env` and benchmark config, validates the current OpenClaw
schema, and verifies eBPF again.

If the repository moved or only its path capitalization changed, setup detects
an invalid old ClawTune link before plugin installation. It backs up
`~/.openclaw/openclaw.json`, runs OpenClaw's repair command, and links the
current checkout. It also refreshes the absolute trusted `launcherPath`; the
sidecar command stays empty and is rediscovered at runtime. Other valid plugin
paths are retained by OpenClaw.

## Replay a SWE-Rebench Trace

Replay reproduces a recorded benchmark interaction without contacting the LLM
provider. Commands, timing options, and output locations are documented in
[SWE-Rebench usage](../swe_rebench/README.md#replay-a-case).
