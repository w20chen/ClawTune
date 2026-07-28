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
  --sample 5
```

### Host OpenClaw Sandbox Mode

The default mode is still `container-openclaw`: each SWE-Rebench task container
runs OpenClaw, the plugin, and the scheduler sidecar inside the image.
In this mode `runtime.stage2_required: false` is propagated explicitly to the
in-container sidecar, so unavailable BCC/eBPF, Docker-event, or cgroup features
degrade to the ordinary tool/resource trace instead of blocking `exec`.
Setting `stage2_required: true` opts into fail-closed startup and final artifact
completeness checks.

To keep OpenClaw on the host and use OpenClaw's Docker sandbox for tools:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --runtime-mode host-openclaw-sandbox \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export
```

`host-openclaw-container` is accepted as a user-facing alias for
`host-openclaw-sandbox`; reports and manifests use the canonical
`host-openclaw-sandbox` value.

This copies `/testbed` from the task image into a host workspace, starts a host
sidecar, configures an isolated OpenClaw home for the task, and mounts the
workspace into the OpenClaw sandbox at `/workspace`.

The swe-rebench task image is tagged as the OpenClaw sandbox image
(`openclaw-sandbox:bookworm-slim`) so that the sandbox inherits all of the
compilers, libraries, and tools that the upstream SWE-Rebench task expects.
If the task image differs from the current sandbox tag, the runner re-tags it
and writes `sandbox-image-build.log` under the task trace directory.

Resource attribution is best-effort:

- `exec` still uses the managed wrapper at `/workspace/.claw/bin/claw-launch`.
- Internal tools such as `read`, `edit`, and `apply_patch` are sampled from the
  shared sandbox container cgroup when it can be discovered.
- Shared sandbox samples are marked
  `coverage_reason: "shared_sandbox_container"` because they are container
  time-window attribution, not exclusive per-tool PID attribution.
- Launcher PIDs without a usable cgroup-v2 child path are container-namespace
  PIDs and are never sampled as host PIDs. The sidecar keeps the discovered
  host-side sandbox cgroup in that case.

### Stage-2 eBPF Clause Telemetry (host-sandbox only)

Stage-2 telemetry uses BCC/BPF to collect **per-clause** `peak_cpu_cores` and
`sampled_peak_rss` via in-kernel perf CPU-clock sampling and kprobe-based
process lifecycle tracking.  This provides much finer-grained resource
attribution than the default cgroup-v2 polling fallback.

**Prerequisites (host machine):**

```bash
# Ubuntu / Debian
sudo apt-get install -y bpfcc-tools python3-bpfcc        \
    linux-headers-$(uname -r) clang llc bpftool

# Verify
python3 -c "from bcc import BPF; print('BCC OK')"
ls /sys/fs/cgroup/cgroup.controllers   # must exist (cgroup v2)
```

**Configuration:**

```yaml
# swe_rebench/config.yaml
runtime:
  mode: "host-openclaw-sandbox"
  stage2_required: true   # enable Stage-2 eBPF telemetry
```

When `stage2_required: true` and the container id is not yet available at claim
time (a brief race in host-sandbox mode), stage2 start is **deferred** rather
than failing the execution.  The sandbox-scope discovery loop retries as soon
as the sandbox container is found, typically within 100–200 ms of OpenClaw
creating it.

**Run:**

```bash
sudo -E env "PATH=$PATH" "$(command -v python3)" \
  -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --runtime-mode host-openclaw-sandbox \
  --dataset swe_rebench/tasks.json \
  --sample 1 --export
```

Passing `--runtime-mode host-openclaw-sandbox` on the CLI makes Stage-2
telemetry required by default. The runner performs a real cgroup/eBPF smoke
collection during preflight and requires a successful exec boundary, non-empty
executable/argv capture, drained lifecycle maps, and zero telemetry loss. It
stops immediately if root/BPF/perf permissions, the toolchain, probe attachment,
or syscall argument decoding are unhealthy. Use `--no-stage2-required` only for
an explicit best-effort diagnostic run; such a run does not satisfy
clause-telemetry completeness.

**Diagnostics:**

- `tool_resource_preflight_host.json` — written to the task trace directory;
  records BCC import status, kernel headers, clang/llc/bpftool availability,
  cgroup-v2 detection, and the semantic smoke result.
- `sidecar-stderr.txt` — check for BPF setup errors (missing kernel headers,
  permission denied, etc.).
- Trace inspection: `python tools/inspect_trace.py <trace.jsonl> --all --details`
  shows per-tool resource telemetry when stage2 is active.
- `llm_proxy_debug_*.json` is automatically written for an empty HTTP-200
  model response. The automatic diagnostic contains byte counts and a SHA-256
  digest, not the raw upstream payload.
- `report.json` keeps `agent_diagnostics` separate from `telemetry_audit`. If
  the model returns no content and executes no tools, telemetry is
  `not_evaluable`; it is not reported as an eBPF collector failure.

**Limitations:**

- Only available in `host-openclaw-sandbox` mode.  `container-openclaw` mode
  cannot load BPF programs inside Docker without `--privileged` and
  `CAP_BPF`/`CAP_SYS_ADMIN`.
- The first tool execution of a session may start stage2 slightly late
  (after sandbox discovery), missing a few hundred ms of early process events.
  Subsequent executions use the already-discovered container id and start
  stage2 immediately.
- The current collector requires effective UID 0 on the host. Its BPF map,
  kprobe/tracepoint, and perf-event setup is not treated as ready from partial
  ambient capabilities alone.
- Every mapped executable clause records `ts_start`, `ts_end`, `latency_ms`,
  and a structured `status` (`exited`, `signaled`, or `unavailable`). The
  contract is `contracts/clause-telemetry.schema.json`.

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
| `--runtime-mode MODE` | Override `runtime.mode`; valid values are `container-openclaw` (default), `host-openclaw-sandbox`, and alias `host-openclaw-container`. |
| `--stage2-required` / `--no-stage2-required` | Require complete eBPF clause artifacts or explicitly allow a best-effort diagnostic run. CLI-selected host-sandbox mode defaults to required. |
| `--dry-run` | Print the selected tasks and exit without pulling images or starting containers. |

Output options:

| Option | Purpose |
| --- | --- |
| `--export` | Copy produced trace JSONL files to `output.flat_export_dir`, usually `swe_rebench/export`. |

Important config-only settings:

| Config key | Purpose |
| --- | --- |
| `runtime.stage2_required` | Require healthy BCC/BPF clause artifacts. Defaults to `false` for the best-effort container mode; host-sandbox CLI selection defaults it to `true`. |
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
