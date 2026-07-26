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

| Option | Purpose |
| --- | --- |
| `--sample N` | Run first N selected tasks. |
| `--skip N` | Skip N tasks before sampling. |
| `--instance-ids a,b` | Run exact task IDs. |
| `--repo owner/repo` | Filter by repo. |
| `--parallelism N` | Run N containers concurrently. |
| `--runtime-mode MODE` | Override `runtime.mode`; use `host-openclaw-sandbox` for host OpenClaw + OpenClaw Docker sandbox. |
| `--export` | Copy traces to `swe_rebench/export`. |
| `--dry-run` | Print selected tasks without starting containers. |
