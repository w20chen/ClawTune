# Run OpenClaw with Strict eBPF Tracing

Complete the [README eBPF-first quick start](../README.md#ebpf-first-quick-start)
before using this guide. In particular:

- `tools/check_stage2.py` must report `"stage2_ready": true`;
- the privileged sidecar must already be running;
- plugin `executionBackend` must be `managed-wrapper`;
- plugin `enableCgroup` must be `true`.

## Configure the Model Proxy

Route OpenClaw model traffic through the sidecar at:

```text
http://127.0.0.1:8765/v1
```

The sidecar forwards the `Authorization` header received from OpenClaw unless
an explicit upstream-key override is configured.

### Direct OpenAI-Compatible Provider

Onboard a vLLM-compatible OpenClaw provider profile, replacing the key and
model:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local \
  --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
```

### OpenRouter or a Different Upstream Model Name

Put the routing values in the repository `.env`, then restart the sidecar:

```bash
AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL=deepseek-v4-flash
AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL=deepseek/deepseek-v4-flash
```

Set `AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE` only when the sidecar must
intentionally use a different key from the one OpenClaw sends. Do not commit
provider keys.

## End-to-End Smoke Test

```bash
openclaw agent --local --agent main --model "vllm/<your-model>" \
  --message "Use the shell to run: python -c 'print(\"stage2-ok\")'. Then summarize the result." \
  --session-key "clawtune-stage2-smoke"
```

Inspect the latest correlated execution:

```bash
curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
ls data/traces/*.jsonl
python tools/inspect_trace.py data/traces/*.jsonl --all --details
```

## What Counts as Success

| Check | Required evidence |
| --- | --- |
| Kernel preflight | `tools/check_stage2.py` exits 0 with `stage2_ready: true` |
| Plugin path | The `exec` call is a managed-wrapper execution with an execution ID |
| Attribution | A dedicated cgroup/process scope is connected to the tool call |
| Stage-2 artifact | The execution references exactly one finalized clause telemetry artifact |
| Collector health | At least one exec boundary and non-empty argv/path; no ring/perf/argv telemetry loss |
| Trace | Model/tool spans are written using trace schema v6 |

`/health/live` and `/health/ready` alone are insufficient: they prove the API
process is alive, not that BPF probes attached or observed a command.

## Cgroup Delegation

`claw-launch` first attempts the configured cgroup root and can retry inside a
transient delegated systemd user scope. If migration still fails with exit code
125, follow the exact
[cgroup troubleshooting procedure](troubleshooting.md#cgroup-problems).

Do not accept process-tree fallback as a successful strict run. The fallback
switches are provided only to isolate unrelated plugin/model problems.

## SWE-Rebench Host Sandbox

For complete benchmark telemetry, use the maintained host route:

```bash
source .venv-system/bin/activate

sudo -E env \
  "PATH=$PATH" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "$(command -v python)" \
  -m swe_rebench.runner run \
  --config swe_rebench/config.yaml \
  --prepare \
  --runtime-mode host-openclaw-sandbox \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export
```

This route exports the task testbed to a host workspace, starts the verified
host sidecar, uses OpenClaw's Docker sandbox for tools, and repeats the strict
semantic preflight before the task starts.

Internal file tools share the sandbox container cgroup and are labelled
`shared_sandbox_container`; managed `exec` calls require their launcher-linked
Stage-2 lifecycle and artifact.

## Troubleshooting

Use [docs/troubleshooting.md](troubleshooting.md) for:

- Conda versus `/usr/bin/python3` package mismatches;
- `bcc` versus openEuler `bpfcc` imports;
- matching `BCC_KERNEL_SOURCE` and kernel headers;
- Linux 6.2+ `rss_stat` compile failures;
- cgroup delegation and launcher exit code 125;
- root-owned `swe_rebench/.runtime` directories;
- BPF compilation succeeding while probe attachment/runtime semantics fail;
- ARM/Kunpeng QEMU setup.
