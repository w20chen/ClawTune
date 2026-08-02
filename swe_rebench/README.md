# Running SWE-Rebench with ClawTune

The batch runner sends SWE-Rebench tasks through OpenClaw while ClawTune records
model calls, tool calls, process lifecycle, CPU, and memory. eBPF is required;
a task stops before agent work if the collector cannot produce valid data.

Complete the root [installation guide](../docs/getting-started.md) first.

## Configure once

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

## Select tasks

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
./.venv/bin/python -m swe_rebench.discover \
  --sample 20 --out swe_rebench/tasks.json
```

Discovery prefers a local agent-test-bench task file and can fall back to its
configured remote dataset source.

## Run

Always start with one task:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

Limit every task to ten minutes (the shorter `--timeout-seconds` alias is also
accepted):

```bash
python3 scripts/clawtune.py benchmark --sample 32 --task-timeout-seconds 600
```

This is a hard wall-clock limit for one task, including all OpenClaw turns and
tool calls. A timed-out task is marked failed and its processes and sandbox are
cleaned up. OpenClaw's supported `agent` CLI exposes a run timeout, but not a
maximum-turn option, so this benchmark uses the reliable time limit.

With an explicit task source:

```bash
python3 scripts/clawtune.py benchmark \
  --dataset swe_rebench/tasks.json \
  --sample 5
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
Selected tasks run one at a time, in that same order.

## Knowledge sharing within a batch

Each benchmark invocation starts a new batch-local tool-resource KB from the
tracked cold-start snapshots. All selected tasks in that invocation share the
same aggregate KB generation serially: a task receives the generation produced
by the preceding task, then its valid updated snapshots become the input to the
next task.

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
namespace. A new benchmark invocation starts a new batch generation rather than
implicitly resuming an older one.

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

Inspect a trace:

```bash
python tools/inspect_trace.py \
  swe_rebench/.runtime/traces/<task-id>/*.jsonl --all --details
```

The report separates agent/provider failures from telemetry failures so an
empty model response is not incorrectly reported as a kernel collector bug.

## Kunpeng notes

Setup registers and smoke-tests amd64 binfmt on arm64. The eBPF sidecar remains
native; only the official amd64 task userspace is emulated. Builds can be much
slower than x86, so increase `batch.task_timeout_seconds` only after a real task
hits the current limit.

Focused QEMU check:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh check
```

See [Kunpeng and arm64](../docs/arm-qemu.md).

## When a task fails

Look inside its trace directory for:

- `tool_resource_preflight_host.json` — kernel collector readiness;
- `sidecar-stderr.txt` — BCC or sidecar errors;
- `openclaw-stderr.txt` — provider/agent errors;
- `sandbox-runtime-preflight.log` — task Python and pip selection.

Then use the symptom-based [troubleshooting guide](../docs/troubleshooting.md).
