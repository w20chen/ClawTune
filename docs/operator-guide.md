# Using ClawTune with OpenClaw

Setup installs/enables the plugin, writes the trusted managed-execution
`launcherPath`, and configures automatic privileged eBPF sidecar startup. The
sidecar command itself stays empty: the plugin derives the current checkout,
`.venv`, matching kernel tree, and `sudo` arguments at runtime.

## Route the Model Through ClawTune

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

## Choose the CLI Lifetime

For normal multi-turn use, keep one Gateway running and connect the TUI:

```bash
# terminal 1
openclaw gateway run

# terminal 2
openclaw tui --session main
```

The useful ownership model is:

```text
Gateway -> agent -> session -> run (one submitted turn)
```

The Gateway and sidecar may be long-lived, while ClawTune finalizes per-run
writer and registry state at `agent_end`. `session_end` supplies cleanup for
older or incomplete hook payloads without a run ID. Ordinary CLI use needs one
Gateway and only modest session concurrency.

Other CLI forms have deliberately narrower lifetimes:

| Command | Use |
| --- | --- |
| `openclaw chat` | Interactive embedded TUI without a Gateway |
| `openclaw agent --local ...` | One embedded, non-interactive turn |
| `python3 scripts/clawtune.py agent --local ...` | One-shot fallback when plugin-started sudo cannot prompt |

## Per-Repository KB Namespaces

Tool-resource knowledge is namespaced per repository. In normal use the plugin
derives the namespace automatically from the working directory of the Gateway
process — git remote `origin` when available, otherwise the directory name — so
starting the Gateway from inside the repository is enough:

```bash
cd /path/to/acme/widgets
openclaw gateway run
```

Override explicitly with `plugins.entries.agent-scheduler.config.repo`, the
`OPENCLAW_AGENT_SCHEDULER_REPO` environment variable, or `CLAW_REPO_KEY`
(highest priority). SWE-Rebench injects `CLAW_REPO_KEY` per task and is
unaffected. See `docs/configuration.md` for the full priority order.


## End-to-End One-Shot Smoke Test

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'. Then summarize it." \
  --session-key "clawtune-smoke"
```

The plugin waits for sidecar readiness before OpenClaw can make its first model
request. A separately running sidecar is reused. The explicit Python wrapper
remains the one-shot fallback for non-interactive sudo environments. This
smoke command is not the recommended ongoing CLI interface.

Inspect the correlated execution:

```bash
curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
python tools/inspect_trace.py data/traces/*.jsonl --all --details
```

A successful run has a model span, a managed tool execution, an attached
cgroup/process scope, a finalized eBPF command artifact with executable/argv
data, and no collector loss. API health alone proves only that the process is
listening; setup/check prove kernel collection.

## Operational Behavior

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

## Run a Benchmark

The two benchmark journeys are batch workflows that run the OpenClaw agent
against many tasks and export traces:

- SWE-Rebench: `python3 scripts/clawtune.py benchmark --sample 1` — each task
  is a repository fix executed in its own task container with Stage-2 eBPF
  clause telemetry. See [SWE-Rebench usage](../swe_rebench/README.md).
- Deep Research Bench: `python3 scripts/clawtune.py drb --sample 1` — each
  task is a research question answered with web-style tools in one very basic
  Docker sandbox; its relaxed telemetry gate requires only LLM +
  resource-sampled tool spans. See
  [Deep Research Bench usage](../deep_research_bench/README.md).

Full walkthroughs are in [getting-started.md](getting-started.md).

## Replay SWE-Rebench Tool Workloads

Replay is a benchmark-analysis workflow, not an interactive OpenClaw mode.
It reads a v6 JSONL trace, uses a local deterministic model endpoint to replay
the recorded LLM turns, and lets the normal OpenClaw runtime issue the
recorded tool calls. The task image, sandbox filesystem, task environment,
launcher, sidecar, cgroup attribution, and eBPF collector are the same ones
used by `host-openclaw-sandbox` benchmark runs.

Run it from the repository root on Linux:

```bash
python3 scripts/clawtune.py replay \
  --dataset /path/to/tasks.json \
  --task-id <instance-id> \
  --trace swe_rebench/.runtime/traces/<instance-id> \
  --timing exact
```

The dataset and trace must describe the same case. The dataset supplies the
Docker image; the trace supplies the LLM/tool sequence. Replay creates a
separate workspace and writes results to
`swe_rebench/replays/<instance-id>/`. It does not call the configured upstream
provider. `--timing none` is useful for smoke tests; `--timing exact` is for
latency-sensitive experiments.

Replay currently supports v6 traces and managed `exec` calls. Missing,
redacted, or ambiguous command input is rejected rather than reconstructed
from predictions or launcher wrappers. Treat source traces as executable
input: review them first and use only disposable workspaces.
