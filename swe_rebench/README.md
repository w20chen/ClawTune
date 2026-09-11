# SWE-Rebench with ClawTune

SWE-Rebench is one adapter behind the common online benchmark workflow. It
runs repository tasks through OpenClaw while ClawTune records model, tool,
process, cgroup, and eBPF clause telemetry.

Complete the root [installation guide](../docs/getting-started.md) first.

## Configure

The public runner reads `configs/benchmark.yaml`. Export the model-provider
key in the launch shell or use the ignored `configs/llm_api_key.txt`:

```bash
export LLM_API_KEY="<provider-api-key>"
```

The files `swe_rebench/config.yaml` and
`swe_rebench/config.example.yaml` belong to the retained internal/legacy
runner. They remain readable with an explicit `--config`, but they do not
define the public runner's learning, parallelism, or output behavior.

## Task source

A task object requires:

- `instance_id` (or `task_id`/`id`);
- `repo`, or a standard `owner__repo-issue` instance ID;
- `problem_statement`;
- dataset-provided `docker_image` (or `image`/`image_name`);
- optional `base_commit`.

The source may be a JSON array, JSONL, or an object containing a `tasks`,
`instances`, or `data` array. With no `--dataset`, selection checks
`$AGENT_TEST_BENCH_ROOT/data/swe-rebench/tasks.json`, the usual sibling
checkout, and finally the bundled four-task smoke source. The external
agent-test-bench checkout is read-only.

Useful selections:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset /data/swe-rebench.jsonl --sample 3 --dry-run

python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset /data/swe-rebench.jsonl \
  --instance-ids django__django-12345,sympy__sympy-67890

python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset /data/swe-rebench.jsonl --repo django/django --sample 5
```

`--sample N` takes the first N tasks after `--instance-ids`, `--repo`,
and `--skip` filtering. It errors if fewer than N tasks remain.

## Run

Start with one task:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 1
```

`--parallelism N` sets the maximum number of tasks in flight; `1` is serial.
There is no synchronization barrier between tasks: a completed worker is
replaced immediately. Tool completions update the shared in-memory predictor
under its lock and enqueue persistence on a single writer, which coalesces
concurrent updates. Per-task cleanup drains only that runtime's executions and
finalizers. After all tasks finish, one global durability barrier commits the
queue before the sidecar stops.

Every invocation initializes a new run-owned KB from `seeds/demo-v1` (or
`--seed`) and never merges it into daily user state or another benchmark run.
Resume is allowed only after a fully saved task boundary:

```bash
python3 scripts/clawtune.py benchmark --resume /path/to/run
```

An interrupted active task cannot be resumed because it may have partially
updated the KB.

### Timeouts

```bash
python3 scripts/clawtune.py benchmark --sample 10 \
  --task-timeout-seconds 600 --agent-timeout-seconds 420
```

The task timeout covers preparation, agent execution, and result collection.
The agent timeout is an optional shorter budget for only the OpenClaw phase;
`0` disables either limit.

## Output and verification

Outputs are owned by the invocation:

```text
.runtime/benchmarks/swe-rebench/<run>/
  run.json
  report.json
  kb/
  sidecar/
  traces/<stable-task-digest>/
  workspaces/<stable-task-digest>/
```

`run.json` records source order, normalized task metadata, configured
parallelism, in-flight task IDs for crash detection, observed shared-KB
generations, final durable generation, errors, and learning status.
Per-task generation intervals are observations of the shared KB and may include
peer updates; they are not causal attribution. `official_score` is `null`: this workflow
measures ClawTune prediction/learning and does not invoke an official SWE
grader.

Inspect a task trace:

```bash
python tools/inspect_trace.py \
  .runtime/benchmarks/swe-rebench/<run>/traces/<task>/*.jsonl --all --details
```

Repository tasks require strict telemetry. A run is not valid unless collector
preflight succeeds, every required managed execution has an exclusive cgroup,
and eBPF clause artifacts pass the telemetry gate.

On Kunpeng, setup registers QEMU for x86_64 task images. The sidecar remains
native; only task userspace is emulated. See [Kunpeng and arm64](../docs/arm-qemu.md).

For all adapter fields and shared KB semantics, see the
[peer benchmark reference](../docs/MULTI_BENCHMARK_IMPLEMENTATION.md).
