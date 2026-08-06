# Running SWE-Rebench with ClawTune

The batch runner sends SWE-Rebench tasks through OpenClaw while ClawTune records
model calls, tool calls, process lifecycle, CPU, and memory. eBPF is required;
a task stops before agent work if the collector cannot produce valid data.

Complete the root [installation guide](../docs/getting-started.md) first.

## Configure Once

`python3 scripts/clawtune.py setup` creates
`swe_rebench/config.yaml`. Export the provider key in the shell that starts the
run:

```bash
export LLM_API_KEY="<provider-api-key>"
```

The wrapper preserves only an explicit environment allow-list through `sudo`;
it does not require `sudo -E`. For a persistent alternative, put the provider
key on one line in the Git-ignored file:

```text
swe_rebench/llm_api_key.txt
```

Edit the model section:

```yaml
llm:
  api_key_file: "./swe_rebench/llm_api_key.txt"
  upstream_base_url: "https://api.deepseek.com"
  model: "your-model-name"
  openclaw_model_ref: "vllm/your-model-name"
```

Keep the runtime, eBPF, cgroup, and privileged Docker defaults. The wrapper
defaults to `linux/amd64` on Kunpeng and leaves x86 native. An exported
`SWE_REBENCH_DOCKER_PLATFORM` takes priority. ClawTune passes this selection to
Docker without adding the unsupported `sandbox.docker.platform` key to
OpenClaw's configuration.

## Select Tasks

An explicit `--dataset` or `--tasks` argument always selects that file. Without
one, the runner uses the first task source that exists, in this order:

1. `$AGENT_TEST_BENCH_ROOT/data/swe-rebench/tasks.json`;
2. the usual sibling checkout,
   `../agent-test-bench/data/swe-rebench/tasks.json`;
3. the bundled `swe_rebench/tasks.json`, which contains only four smoke-test
   tasks.

The bundled file is intentionally only a fallback. For example, `--sample 32`
can select 32 tasks only when the environment or sibling source contains at
least that many tasks. The runner fails before execution when a positive
`--sample N` requests more matching tasks than the selected source provides;
it never silently runs a smaller batch.

You can also pass a JSON/JSONL dataset directly. To create a smaller task list
with the discovery helper:

```bash
python3 -m swe_rebench.discover \
  --sample 20 --out swe_rebench/tasks.json
```

Discovery prefers a local agent-test-bench task file and can fall back to its
configured remote dataset source.

Example: generate a 128-task source first, then run it (don't overwrite the
bundled 4-task smoke-test fallback — write to a separate file):

```bash
# 1) Generate the task source (agent-test-bench checkout, else HuggingFace)
python3 -m swe_rebench.discover \
  --sample 128 --out swe_rebench/tasks-128.json

# 2) Run the benchmark against it
python3 scripts/clawtune.py benchmark \
  --dataset swe_rebench/tasks-128.json \
  --sample 128 --parallelism 8
```

Other useful selections:

```bash
# Exact instances
python3 scripts/clawtune.py benchmark \
  --dataset swe_rebench/tasks.json \
  --instance-ids django__django-12345,sympy__sympy-67890

# One repository
python3 scripts/clawtune.py benchmark \
  --dataset swe_rebench/tasks.json \
  --repo django/django --sample 5

# Show the selection without executing tasks
python3 scripts/clawtune.py benchmark \
  --dataset swe_rebench/tasks.json \
  --sample 3 --dry-run
```

`--sample N` means the first `N` tasks in source order after any
`--instance-ids`, `--repo`, and `--skip` filtering. It is not a random sample.
The report preserves that selection order. Execution is serial by default and
becomes concurrent only when `--parallelism` or `batch.parallelism` is greater
than one.

## Run

Always start with one task:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

## Timeouts

Limit every task to ten minutes (the shorter `--timeout-seconds` alias is also
accepted):

```bash
python3 scripts/clawtune.py benchmark --sample 32 --task-timeout-seconds 600
```

This is a hard wall-clock limit for one task, including all OpenClaw turns and
tool calls, repository export, preflight, and normal result collection. The
remaining budget, rather than the original value, is passed to the agent after
setup. A timed-out task exits with status 124. Agent and sandbox termination
then use a separate bounded cleanup grace, so safe cleanup is still attempted
after the task deadline has expired.

To impose an additional shorter limit on only the OpenClaw phase:

```bash
python3 scripts/clawtune.py benchmark --sample 32 \
  --task-timeout-seconds 600 --agent-timeout-seconds 420
```

The effective agent limit is the smaller of the agent limit and the remaining
whole-task budget. `0` disables the corresponding limit. OpenClaw's supported
`agent` CLI does not expose a maximum-turn option, so the runner enforces time
budgets at the process boundary.

## Replay a Case

Replay is currently supported for one current-format JSONL trace in the
`host-openclaw-sandbox` topology. It uses the same task image, `/testbed`
export, OpenClaw Docker sandbox, task Python environment, launcher, sidecar,
cgroup scope, and eBPF exec-clause path as a normal benchmark case. Only the
model provider is replaced by a local deterministic server: recorded LLM turns sleep
for their recorded duration and return the recorded tool calls; `exec` tools
then run normally on the task image's CPU environment.

The source trace is never modified. Replay outputs are written below
`swe_rebench/replays/<task-id>/`, including a new JSONL trace, resource artifacts,
`replay_manifest.json`, and logs. A source trace with redacted or missing tool
arguments is rejected rather than executing an ambiguous command.

### Prerequisites

Replay runs on the Linux host, not on Windows or macOS. Complete the root
[installation guide](../docs/getting-started.md), run the eBPF check, and
ensure the task dataset and source trace refer to the same SWE-Rebench case.
The dataset is required because it supplies the task image; the trace alone
does not contain the complete image filesystem or installed dependencies.

The source trace must be a ClawTune JSONL trace. Locate a completed case
under the benchmark output directory, for example:

```text
swe_rebench/.runtime/traces/<task-id>/*.jsonl
```

Run the first replay with the same `host-openclaw-sandbox` mode used by the
benchmark and with `--timing none` to reduce the smoke-test duration:

```bash
python3 scripts/clawtune.py replay \
  --dataset /path/to/tasks.json \
  --task-id django__django-12345 \
  --trace swe_rebench/.runtime/traces/django__django-12345 \
  --timing none
```

The lower-level runner is also available when the `.venv` and privilege
environment are already configured:

```bash
python3 -m swe_rebench.runner replay \
  --config swe_rebench/config.yaml \
  --dataset /path/to/tasks.json \
  --task-id django__django-12345 \
  --trace swe_rebench/.runtime/traces/django__django-12345 \
  --timing exact
```

### Timing modes

- `--timing exact`: sleep for each recorded LLM duration;
- `--timing scale --timing-scale 0.1`: sleep for 10% of each recorded LLM
  duration;
- `--timing none`: do not wait for recorded LLM latency.

Timing applies only to simulated LLM calls. Tool calls are executed normally
and their replay duration is measured again. Replay does not call the original
LLM provider and does not reuse source-trace resource values.

### Outputs and verification

Check the replay directory after a run:

```bash
find swe_rebench/replays/django__django-12345 -maxdepth 2 -type f | sort
cat swe_rebench/replays/django__django-12345/replay_manifest.json
python tools/inspect_trace.py \
  swe_rebench/replays/django__django-12345/*.jsonl --all --details
```

It contains a new JSONL trace, a new `tool-resource/` artifact directory,
`replay_manifest.json`, and sidecar/sandbox logs. Compare the replay
`resources` and `execution.tool_resource` fields with the source trace only as
two separate measurements; do not overwrite or merge them.

Replay fails closed for unsupported runtime modes, older-format traces,
incomplete LLM spans, or tool arguments that were redacted/truncated. The first
Linux acceptance run should use a harmless trace and verify the manifest, a new
JSONL file, and one exec-clause artifact before replaying a real case.

### Safety and cleanup

Replay executes commands recorded in the source trace. Review the trace before
running it, use a disposable task workspace, and keep the normal sandbox and
network policy enabled. Do not replay untrusted traces on a host with access
to sensitive files or credentials. The replay workspace is separate from the
original benchmark workspace.

## Run Cases Concurrently

First prove the complete path with one serial case, then increase concurrency
gradually:

```bash
python3 scripts/clawtune.py benchmark --sample 1 --parallelism 1
python3 scripts/clawtune.py benchmark --sample 8 --parallelism 4
python3 scripts/clawtune.py benchmark --sample 32 --parallelism 16
```

For a sufficiently provisioned large host, a 128-case run is:

```bash
python3 scripts/clawtune.py benchmark --sample 128 --parallelism 128
```

`--sample` is the batch size; `--parallelism` is the maximum number of cases
executing simultaneously. The command-line value overrides
`batch.parallelism` in `swe_rebench/config.yaml`. The default is `1`, requiring
an explicit choice before a large workload.

The batch starts exactly one host Sidecar and gives every case a distinct
Gateway/runtime/session/run identity, worktree, and cgroup. Independent
OpenClaw processes share the Sidecar without sharing execution state. The
Sidecar derives usable CPU capacity from affinity and cgroup limits, reserves
host capacity, and applies weighted admission using predicted CPU demand.
Consequently, `128` is neither a universal safe value nor a hardcoded limit;
also account for memory, Docker I/O, provider quota, and QEMU overhead.

This benchmark concurrency should not be copied into the normal-user topology.
Routine CLI use is one Gateway with a small number of sessions. Benchmark mode
uses independent task runtimes because it is a batch harness, while reusing the
same Plugin/Sidecar protocol.

## Knowledge Sharing within a Batch

Each benchmark invocation starts a new batch-local tool-resource KB from the
tracked cold-start snapshots. All selected tasks contribute to the same
aggregate KB. In a serial run, the next task sees the preceding task's
published generation. In a concurrent run, tasks may start from the same
generation; their valid updates are serialized by the Sidecar KB writer and
merged at the batch barrier instead of overwriting one another. Runtime drain
waits for pending KB work before result collection.

The aggregate snapshots contain two intentionally different layers:

- `public` is the frozen, coarse cold-start prior shared by every repository.
  Task execution does not add online observations to this layer.
- `repo` contains causally accumulated online evidence under separate
  repository keys. A later task for `12rambau/sepal_ui`, for example, can use
  observations from earlier tasks for that repository. A task from another
  repository cannot use those repo-specific observations and falls back to its
  own namespace or the frozen public prior.

Thus, "updating the shared KB" means publishing the complete aggregate
snapshot; the task's new online evidence is written only to its own `repo`
namespace. There is currently no per-session privacy boundary: sessions in the
batch may use the shared aggregate under these public/repository lookup rules.
A new benchmark invocation starts a new batch generation rather than implicitly
resuming an older one.

The dataset's non-empty `repo` or `repository` metadata is authoritative for
the namespace. If it is absent, a standard instance ID such as
`12rambau__sepal_ui-411` is interpreted as `12rambau/sepal_ui`. If neither form
can identify a repository, the runner uses the isolated
`instance:<instance-id>` key instead of mixing unrelated tasks. The resolved
key is recorded as `repo` in each task's `task_manifest.json`.

The wrapper always refreshes a stale runtime bundle and exports results. It
also supplies the verified Python, BCC, kernel, sudo, and architecture settings
used by setup; no activation or manual environment exports are needed.

## Output

Generated data is Git-ignored under:

```text
swe_rebench/.runtime/
  traces/<task-id>/
  kb-batches/<batch-id>/
    _sidecar/                       # shared sidecar: in-flight per-runtime JSONL
    clause-resource-kb.json
    runtime-tool-resource-kb.json
  export/
  report.json
```

Each task trace also retains the KB snapshots used and updated by that task
under `traces/<task-id>/tool-resource/`. The batch directory above is the
auditable final aggregate generation, and its exact path is recorded as
`shared_kb_dir` in `report.json`. The maintained host-sandbox mode also records
it in each task manifest.

With the shared batch sidecar, trace JSONL files are named
`<runtime_id>__<session>_<run>__<digest>.jsonl` (`runtime_id` is the opaque
`claw-srb-<hash>`). The runner labels collected copies with the case id, so
`traces/<task-id>/` contains `<task-id>__<runtime_id>__...jsonl`. While a batch
is still running, `kb-batches/<batch-id>/_sidecar/runtime-case-map.json` maps
each in-flight `runtime_id` to its case id so traces are attributable before
collection finishes.

Inspect a trace:

```bash
python tools/inspect_trace.py \
  swe_rebench/.runtime/traces/<task-id>/*.jsonl --all --details
```

The report separates agent/provider failures from telemetry failures so an
empty model response is not incorrectly reported as a kernel collector bug.
The complete report is always saved to `report.json`; normal terminal output is
only the compact progress summary. Pass `--json` when a caller explicitly
wants the complete report JSON on stdout:

```bash
python3 scripts/clawtune.py benchmark --sample 1 --json
```

## Kunpeng Notes

Setup registers and smoke-tests amd64 binfmt on arm64. The eBPF sidecar remains
native; only the official amd64 task userspace is emulated. Builds can be much
slower than x86, so increase `batch.task_timeout_seconds` only after a real task
hits the current limit.

Focused QEMU check:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh check
```

See [Kunpeng and arm64](../docs/arm-qemu.md).

## When a Task Fails

Look inside its trace directory for:

- `tool_resource_preflight_host.json` — kernel collector readiness;
- `sidecar-stderr.txt` — BCC or sidecar errors;
- `openclaw-stderr.txt` — provider/agent errors;
- `sandbox-runtime-preflight.log` — task Python and pip selection.

Then use the symptom-based [troubleshooting guide](../docs/troubleshooting.md).
