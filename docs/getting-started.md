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

At the end, setup runs a real validation: compile the complete BPF program,
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
the long-running sidecar on demand.

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

With the usual sibling `agent-test-bench` task checkout:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

Or name a task source:

```bash
python3 scripts/clawtune.py benchmark \
  --dataset /path/to/tasks.json \
  --instance-ids django__django-12345
```

Start with one task. QEMU runs on Kunpeng are slower than native x86_64 runs,
so increase `batch.task_timeout_seconds` in the benchmark config only when a
real task reaches the default deadline.

After that succeeds, increase concurrency explicitly:

```bash
python3 scripts/clawtune.py benchmark --sample 8 --parallelism 4
```

One batch uses one host Sidecar and one shared batch KB while keeping every
task's runtime identity, worktree, trace output, and cgroup separate. `--sample`
controls selected cases; `--parallelism` controls simultaneous cases. The
default parallelism is `1`. Choose a higher value based on CPU, memory,
Docker/QEMU throughput, and provider quota. Running 128 selected cases with
parallelism 128 is for a host validated at that load, not a universal default.

## Run Deep Research Bench

Deep Research Bench runs PhD-level research questions through OpenClaw while
ClawTune records the same model/tool/resource telemetry. There is no per-task
image and no `/testbed` repository: the agent's tools execute in one very basic
Docker sandbox image (default `python:3.11-slim`, configurable in
`deep_research_bench/config.yaml` under `sandbox.image`).

A bundled three-task smoke source is used when no dataset is named:

```bash
python3 scripts/clawtune.py drb --sample 1 --parallelism 1
```

Build a larger task source from the HuggingFace dataset
(`muset-ai/DeepResearch-Bench-Dataset`, `generated_reports/openai-deepresearch.jsonl`):

```bash
python3 -m deep_research_bench.discover --sample 32 --out deep_research_bench/tasks-32.json
python3 scripts/clawtune.py drb --dataset deep_research_bench/tasks-32.json --sample 32
```

Per-task output lands under `deep_research_bench/.runtime/traces/<task-id>/`
(v6 trace, `agent_prompt.txt`, `task_manifest.json`, record-only
`reference_answer.txt`, `result_summary.json`); the batch report is written to
`deep_research_bench/.runtime/report.json`. Research tools are measured with the
sandbox-container / per-PID scope, so the relaxed telemetry gate
(`runtime.gate_required`, default `true`) requires only an LLM span and a
resource-sampled tool span per task — never Stage-2 exec clauses. See
[Deep Research Bench usage](../deep_research_bench/README.md).

The agent answers with OpenClaw's built-in `web_search` tool (Tavily by
default). Export `TAVILY_API_KEY` in the launch shell (the wrapper allows it
through `sudo`), or put it on one line in
`deep_research_bench/tavily_api_key.txt`. Web search runs on the host, so the
key does not need to reach the sandbox. See
[Web Search (Tavily)](../deep_research_bench/README.md#web-search-tavily).

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

After one benchmark case has completed, you can replay its recorded
interaction without contacting the LLM provider. Replay uses the original
case's Docker image and the same `host-openclaw-sandbox` setup as a normal
benchmark, so the task filesystem and installed Python dependencies are
recreated from the image. The recorded LLM durations are simulated locally;
recorded `exec` tools are sent through OpenClaw's normal sandbox, ClawTune's
managed launcher, and the existing cgroup/eBPF telemetry path.

Replay requires all of the normal Linux prerequisites: Docker, cgroup v2,
BCC/eBPF permissions, matching kernel headers, and the prepared `.venv`.
The task dataset is required to resolve the case image, and the source must be
a ClawTune v6 JSONL trace from that same case.

Use the unified wrapper so the required interpreter and privileged runtime are
selected automatically:

```bash
python3 scripts/clawtune.py replay \
  --dataset /path/to/tasks.json \
  --task-id django__django-12345 \
  --trace swe_rebench/.runtime/traces/django__django-12345 \
  --timing none
```

Start with `--timing none` for a short smoke test. Use `--timing exact` to
preserve the recorded LLM wait time, or
`--timing scale --timing-scale 0.1` to preserve only 10% of it. Tool latency
and resource usage are measured during replay and are not copied from the
source trace.

Replay results are stored under
`swe_rebench/replays/<task-id>/`. Inspect `replay_manifest.json`, the new
JSONL trace, and `tool-resource/` artifacts. Review commands in the source
trace before replaying it; replay executes recorded commands in a disposable
task workspace and must not be used with untrusted traces on a sensitive host.

For all options and lower-level runner usage, see
[SWE-Rebench usage](../swe_rebench/README.md#replay-a-case).
