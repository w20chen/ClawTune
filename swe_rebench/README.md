# Running SWE-Rebench with ClawTune

The batch runner sends SWE-Rebench tasks through OpenClaw while ClawTune records
model calls, tool calls, process lifecycle, CPU, and memory. eBPF is required;
a task stops before agent work if the collector cannot produce valid data.

Complete the root [installation guide](../docs/getting-started.md) first.

## Configure once

`python3 scripts/clawtune.py setup` creates
`swe_rebench/config.yaml`. Put the provider key on one line in the Git-ignored
file:

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
chooses `linux/amd64` automatically on Kunpeng and leaves x86 native.

## Select tasks

The runner automatically checks either `AGENT_TEST_BENCH_ROOT` or the usual
sibling checkout:

```text
../agent-test-bench/data/swe-rebench/tasks.json
```

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

The wrapper always refreshes a stale runtime bundle and exports results. It
also supplies the verified Python, BCC, kernel, sudo, and architecture settings
used by setup; no activation or manual environment exports are needed.

## Output

Generated data is Git-ignored under:

```text
swe_rebench/.runtime/
  traces/<task-id>/
  export/
  report.json
```

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
