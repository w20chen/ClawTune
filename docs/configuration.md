# Configuration

ClawTune creates sensible defaults during `python3 scripts/clawtune.py setup`.
Most users only configure a provider key and model. Keep all paths relative to
the repository unless you intentionally manage data elsewhere.

## Sidecar and OpenClaw

The root `.env` controls the long-running sidecar. Useful settings are:

| Setting | Default | When to change it |
| --- | --- | --- |
| `AGENT_SCHEDULER_DB_PATH` | `data/openclaw-trace.sqlite3` | Move persistent scheduler state |
| `AGENT_SCHEDULER_TRACE_DIR` | `data/traces` | Move OpenClaw trace output |
| `AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL` | DeepSeek API | Use another OpenAI-compatible provider |
| `AGENT_SCHEDULER_TOKEN` | unset | Require local sidecar authentication |

The eBPF collector is required by default. Do not disable it for a result that
will be treated as a valid ClawTune measurement.

## Benchmark Runs

The setup command copies `swe_rebench/config.example.yaml` once. Exporting a
key in the launch shell is the simplest secret configuration:

```bash
export LLM_API_KEY="<provider-api-key>"
```

The unified benchmark wrapper preserves `LLM_API_KEY` through `sudo` by name
only; it neither uses broad `sudo -E` nor puts the secret value in process
arguments. If the environment variable is unset, the runner falls back to the
configured key file and then to `LLM_API_KEY` in the root `.env`.

The values a new user normally edits are:

```yaml
llm:
  api_key_file: "./swe_rebench/llm_api_key.txt"
  upstream_base_url: "https://api.deepseek.com"
  model: "your-model-name"
  openclaw_model_ref: "vllm/your-model-name"

batch:
  task_timeout_seconds: 1800
  agent_timeout_seconds: 0
  parallelism: 1
```

`task_timeout_seconds` is the whole-task wall-clock budget, beginning before
repository export and preflight. `agent_timeout_seconds` optionally adds a
shorter agent-only limit; `0` disables that separate limit. After either limit
fires, process and sandbox termination use a small independent cleanup grace
instead of reusing an already exhausted task budget.

`parallelism` is the maximum number of benchmark cases executing at once.
`--parallelism N` overrides it for one invocation. Keep `1` for the first
acceptance run, then increase gradually. It is independent of `--sample`, which
controls how many tasks are selected. Concurrent runtimes share one Sidecar and
one batch KB.

The runner always writes the full batch report to `output.report_path`. It
prints only compact progress by default; pass `--json` to also emit the full
report on stdout.

### Benchmark Knowledge Bases

Each benchmark task uses three JSON knowledge bases under `tool-resource/`:

- `runtime-tool-resource-kb.json` stores whole tool/command observations used
  to predict latency, CPU, and memory.
- `clause-resource-kb.json` stores shell-clause observations used for
  clause-level resource prediction.
- `clause-lattice-time-kb.json` stores the eligible eBPF clause observations
  and pending causal updates shared by the `shrinkage`, `loso`, and
  `max_cardinality` clause-time predictors.

`kb-batches/<batch-id>/` contains the batch's shared, evolving snapshot.
Each `traces/<task-id>/tool-resource/` directory contains that task's working
snapshot. KB updates use a single writer. In serial mode, a completed task's
generation becomes the next task's input. In concurrent mode, overlapping
tasks may begin from the same generation and their valid updates are merged at
the synchronization barrier instead of replacing each other.

The runtime and clause-resource files contain a shared `public` namespace and
repo-specific knowledge under `repo`, such as
`repo["12rambau/sepal_ui"]`. The lattice-time file deliberately does not use
that hierarchy. It stores one flat observation corpus, from which one node
mapping is rebuilt; common context and context containing the optional
repository feature coexist and are available to all three algorithms. Files
named `call_*.json` are per-call eBPF telemetry evidence used to update the
KBs; they are not additional knowledge bases.

The lattice snapshot retains observation multiplicity. When Stage-2 history is
replayed at startup, multiset merging removes only occurrences already present
in the snapshot; genuinely repeated executions remain separate samples even
for legacy artifacts whose fallback timestamps are identical.

Keep the host-sandbox runtime, eBPF requirement, privileged cgroup access, and
bundle paths at their defaults. The unified benchmark command defaults to
`linux/amd64` on Kunpeng and leaves the platform native on x86. Export
`SWE_REBENCH_DOCKER_PLATFORM` only when an explicit override is needed; an
environment value takes priority over `docker.platform` in this file.

OpenClaw 2026.7.x does not accept an
`agents.defaults.sandbox.docker.platform` key. ClawTune therefore keeps that
key out of OpenClaw JSON and passes the selected platform to its Docker calls
and child environment instead. Setup validates the resulting OpenClaw config.

The API key file, `.env`, generated runtime bundle, traces, and reports are
Git-ignored.

## OpenClaw Plugin

Setup installs and patches the plugin with:

- local sidecar endpoint `http://127.0.0.1:8765`;
- an absolute managed-execution launcher from the repository `.venv`;
- cgroup tracking enabled;
- automatic sidecar startup with an empty `sidecarCommand`.

An empty `sidecarCommand` is intentional, not a missing configuration. At
runtime the plugin finds the checkout relative to its loaded package and
resolves `.venv`, `.env`, `BCC_KERNEL_SOURCE` (or the running kernel's build
link), a conservative executable path, and `sudo` arguments. Moving the
checkout therefore does not leave a persisted absolute sidecar shell command.

`launcherPath` serves a different purpose: it is the trusted absolute boundary
for managed, instrumented tool execution. An absolute path is a security
requirement, not a host-specific literal in source. Rerunning setup after a
move refreshes it. The pre-agent gate waits for a health response with the
expected ClawTune service and schema identity before OpenClaw can contact the
local model proxy. Normal interactive use can therefore run
`openclaw gateway run` and attach `openclaw tui --session main`; the Gateway
keeps the plugin and sidecar available across turns. Use `openclaw chat` for an
embedded local TUI, `agent --local` for one-shot execution, or the explicit
ClawTune `agent`/`sidecar` wrappers where sudo cannot prompt on a controlling
terminal.

The default sidecar startup window is 60 seconds so a Kunpeng cold start and
an interactive sudo prompt do not consume the old 15-second limit. Advanced
deployments can set `plugins.entries.agent-scheduler.config.sidecarStartupTimeoutMs`
between 1,000 and 600,000 milliseconds; the pre-agent hook always receives an
additional five-second margin.

OpenClaw provider traffic should use `http://127.0.0.1:8765/v1`. The plugin's
full schema is in `packages/openclaw-plugin/openclaw.plugin.json`; values not
covered here are advanced/developer options.
