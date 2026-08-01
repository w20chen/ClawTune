# Configuration

ClawTune creates sensible defaults during `python3 scripts/clawtune.py setup`.
Most users only configure a provider key and model. Keep all paths relative to
the repository unless you intentionally manage data elsewhere.

## `.env`: sidecar and OpenClaw

The root `.env` controls the long-running sidecar. Useful settings are:

| Setting | Default | When to change it |
| --- | --- | --- |
| `AGENT_SCHEDULER_DB_PATH` | `data/openclaw-trace.sqlite3` | Move persistent scheduler state |
| `AGENT_SCHEDULER_TRACE_DIR` | `data/traces` | Move OpenClaw trace output |
| `AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL` | DeepSeek API | Use another OpenAI-compatible provider |
| `AGENT_SCHEDULER_TOKEN` | unset | Require local sidecar authentication |

The eBPF collector is required by default. Do not disable it for a result that
will be treated as a valid ClawTune measurement.

## `swe_rebench/config.yaml`: benchmark runs

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
```

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

## OpenClaw plugin

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
local model proxy. Interactive use can therefore run `openclaw agent ...`
directly. Use the explicit `agent` wrapper or `sidecar` command where sudo
cannot prompt on a controlling terminal.

The default sidecar startup window is 60 seconds so a Kunpeng cold start and
an interactive sudo prompt do not consume the old 15-second limit. Advanced
deployments can set `plugins.entries.agent-scheduler.config.sidecarStartupTimeoutMs`
between 1,000 and 600,000 milliseconds; the pre-agent hook always receives an
additional five-second margin.

OpenClaw provider traffic should use `http://127.0.0.1:8765/v1`. The plugin's
full schema is in `packages/openclaw-plugin/openclaw.plugin.json`; values not
covered here are advanced/developer options.
