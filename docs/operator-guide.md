# Using ClawTune with OpenClaw

Setup installs/enables the plugin, writes its absolute launcher path, and
configures automatic privileged eBPF sidecar startup.

## Route the model through ClawTune

OpenClaw must use the sidecar's OpenAI-compatible proxy so model and tool spans
share one trace:

```text
http://127.0.0.1:8765/v1
```

One non-interactive provider setup is:

```bash
openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local \
  --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:8765/v1" \
  --custom-api-key "<provider-api-key>" \
  --custom-model-id "<model>"
```

The sidecar forwards OpenClaw's authorization header. For a provider other than
the `.env` default, set its upstream base URL there and restart the sidecar:

```bash
AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL=your-visible-model
AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL=provider/real-model
```

Only use the explicit upstream-key override when the proxy must intentionally
use a different credential than OpenClaw. Do not commit keys.

## End-to-end smoke test

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'. Then summarize it." \
  --session-key "clawtune-smoke"
```

The plugin waits for sidecar readiness before OpenClaw can make its first model
request. A separately running sidecar is reused. The explicit Python wrapper
remains the fallback for non-interactive sudo environments.

Inspect the correlated execution:

```bash
curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
python tools/inspect_trace.py data/traces/*.jsonl --all --details
```

A successful run has a model span, a managed tool execution, an attached
cgroup/process scope, a finalized eBPF command artifact with executable/argv
data, and no collector loss. API health alone proves only that the process is
listening; setup/check prove kernel collection.

## Operational behavior

- ClawTune observes by default. Scheduling/placement recommendations do not
  forcibly move work in the current release.
- Managed shell calls use a dedicated launcher so instrumentation is armed
  before short-lived commands begin.
- Built-in file tools may share the sandbox container cgroup; the trace labels
  that boundary instead of claiming exclusive attribution.
- The sidecar persists learned command-resource evidence and can reuse it after
  restart.

See [configuration](configuration.md) for settings and
[troubleshooting](troubleshooting.md) for symptom-based recovery.
