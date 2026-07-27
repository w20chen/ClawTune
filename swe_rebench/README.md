# SWE-Rebench Batch Runner

Use this when you want to run many SWE-Rebench tasks through OpenClaw with the
agent-scheduler plugin and sidecar tracing enabled.

## Setup

```bash
cp swe_rebench/config.example.yaml swe_rebench/config.yaml
# Edit llm.api_key, or keep api_key: "${LLM_API_KEY}" and export LLM_API_KEY.
# Alternatively, put the raw key in swe_rebench/llm_api_key.txt (gitignored).

python -m swe_rebench.runner prepare --config swe_rebench/config.yaml
```

Default provider config is DeepSeek:

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  api_key_file: "./swe_rebench/llm_api_key.txt"
  upstream_base_url: "https://api.deepseek.com"
  model: "deepseek-v4-flash"
  openclaw_model_ref: "vllm/deepseek-v4-flash"
```

OpenRouter example:

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  upstream_base_url: "https://openrouter.ai/api/v1"
  model: "deepseek/deepseek-v4-flash"
  openclaw_model_ref: "vllm/deepseek-v4-flash"
```

## Discover Tasks

```bash
python -m swe_rebench.discover --sample 20 --out swe_rebench/tasks.json
```

Discovery first checks `AGENT_TEST_BENCH_ROOT/data/swe-rebench/tasks.json` or
`../agent-test-bench/data/swe-rebench/tasks.json`. If no local task file is
found, it falls back to the HuggingFace dataset path.

Useful filters:

```bash
python -m swe_rebench.discover --repo django/django --sample 10 --out django-tasks.json
python -m swe_rebench.discover --instance-ids django__django-12345 --out one-task.json
```

## Run A Batch

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 10 \
  --parallelism 4 \
  --export
```

Dry-run task selection:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --skip 10 \
  --sample 3 \
  --dry-run
```

Run exact instances:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --instance-ids django__django-12345,sympy__sympy-67890 \
  --export
```

Run a repo subset:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --repo django/django \
  --sample 5 \
  --parallelism 2
```

### Host OpenClaw Sandbox Mode

The default mode is still `container-openclaw`: each SWE-Rebench task container
runs OpenClaw, the plugin, and the scheduler sidecar inside the image.

To keep OpenClaw on the host and use OpenClaw's Docker sandbox for tools:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --runtime-mode host-openclaw-sandbox \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --parallelism 1 \
  --export
```

This copies `/testbed` from the task image into a host workspace, starts a host
sidecar, configures an isolated OpenClaw home for the task, and mounts the
workspace into the OpenClaw sandbox at `/workspace`.

If the default OpenClaw sandbox image (`openclaw-sandbox:bookworm-slim`) is
missing, the runner builds the minimal npm-install compatible image documented
by OpenClaw and writes `sandbox-image-build.log` under the task trace directory.

Resource attribution is best-effort:

- `exec` still uses the managed wrapper at `/workspace/.claw/bin/claw-launch`.
- Internal tools such as `read`, `edit`, and `apply_patch` are sampled from the
  shared sandbox container cgroup when it can be discovered.
- Shared sandbox samples are marked
  `coverage_reason: "shared_sandbox_container"` because they are container
  time-window attribution, not exclusive per-tool PID attribution.

## Run One Task

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --image swebrebench/sweb.eval.x86_64.django:latest \
  --task-id django__example \
  --problem "Fix the bug described by the benchmark task."
```

## Outputs

```text
swe_rebench/
  traces/<task_id>/*.jsonl
  export/
  report.json
```

Inspect:

```bash
python tools/inspect_trace.py swe_rebench/traces/<task_id>/<trace-file>.jsonl --all --details
python tools/inspect_trace.py swe_rebench/traces/<task_id>/<trace-file>.jsonl --all --timeline
```

Collect/export existing traces:

```bash
python -m swe_rebench.runner collect --config swe_rebench/config.yaml
```

## Common Options

`--config` can be placed before or after the subcommand:

```bash
python -m swe_rebench.runner --config swe_rebench/config.yaml run ...
python -m swe_rebench.runner run --config swe_rebench/config.yaml ...
```

### `prepare`

Builds the runtime bundle mounted into task containers.

| Option | Purpose |
| --- | --- |
| `--config PATH` | Load runner settings from this YAML file. |
| `--bundle-dir PATH` | Override `bundle.output_dir` for this prepare run. |

### `run`

Runs one or more SWE-Rebench tasks. At least one task source must be available:
`--dataset`, `--tasks`, `--image`, or the default discovered task file.

Task source options:

| Option | Purpose |
| --- | --- |
| `--dataset PATH` | Load a SWE-bench/SWE-Rebench JSON or JSONL dataset. |
| `--tasks PATH` | Load a simple task-list JSON file, such as one written by `swe_rebench.discover`. |
| `--image IMAGE` | Run one Docker image directly, bypassing dataset loading. Use with `--task-id` and `--problem`. |
| `--task-id ID` | Task ID for `--image` mode; defaults to `task-1`. |
| `--problem TEXT` | Problem statement for `--image` mode. |

Task selection options:

| Option | Purpose |
| --- | --- |
| `--sample N` | Run only the first N selected tasks after filtering and skipping. |
| `--skip N` | Skip the first N selected tasks before applying `--sample`. |
| `--instance-ids a,b` | Run exact task IDs, preserving the comma-separated order. |
| `--repo owner/repo` | Run only tasks whose `repo` field matches this value. |

Execution options:

| Option | Purpose |
| --- | --- |
| `--prepare` | Rebuild the runtime bundle before running tasks. The runner also rebuilds automatically when the bundle looks stale. |
| `--parallelism N` | Override `batch.parallelism` from config for this run. |
| `--runtime-mode MODE` | Override `runtime.mode`; valid values are `container-openclaw`(default) and `host-openclaw-sandbox`. |
| `--dry-run` | Print the selected tasks and exit without pulling images or starting containers. |

Output options:

| Option | Purpose |
| --- | --- |
| `--export` | Copy produced trace JSONL files to `output.flat_export_dir`, usually `swe_rebench/export`. |

Important config-only settings:

| Config key | Purpose |
| --- | --- |
| `batch.task_timeout_seconds` | Per-task wall-clock timeout. `0` disables the timeout. |
| `batch.retry_failed` | Number of retries after a failed task. |
| `docker.pull_policy` | Image pull behavior: `missing`, `always`, or `never`. |
| `docker.memory_limit` / `docker.cpus` | Per-container resource limits. |
| `docker.network_mode` / `docker.dns_servers` | Container networking controls. |
| `docker.privileged`, `docker.cgroupns_mode`, `docker.cgroup_mount_rw` | Optional cgroup access knobs for more complete cgroup sampling. |
| `docker.cgroup_required` | If `true`, fail hard when per-tool cgroups cannot be created. Keep `false` for broad compatibility. |
| `output.trace_root` | Per-task artifact directory root. |
| `output.report_path` | Batch report JSON path. |
| `output.flat_export_dir` | Destination for `--export`; empty disables flat export. |
| `agent.max_turns` / `agent.extra_args` | Agent behavior defaults used by generated runtime scripts. |

### `collect`

Scans an existing trace root and rewrites the summary report without starting
containers.

| Option | Purpose |
| --- | --- |
| `--config PATH` | Load output paths from this YAML file. |
| `--export-dir PATH` | Override `output.flat_export_dir` for this collect run. |

### `cleanup`

Currently a no-op because task containers are removed after each run.
