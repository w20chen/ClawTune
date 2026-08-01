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

The setup command copies `swe_rebench/config.example.yaml` once. The values a
new user normally edits are:

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
bundle paths at their defaults. The unified benchmark command sets
`linux/amd64` automatically on Kunpeng and leaves the platform native on x86.

The API key file, `.env`, generated runtime bundle, traces, and reports are
Git-ignored.

## OpenClaw plugin

Setup installs and patches the plugin with:

- local sidecar endpoint `http://127.0.0.1:8765`;
- managed launcher from the repository `.venv`;
- cgroup tracking enabled;
- sidecar auto-start disabled.

Auto-start stays disabled because an ordinary Node child cannot acquire the
kernel permissions required by eBPF. Start it with
`python3 scripts/clawtune.py sidecar` instead.

OpenClaw provider traffic should use `http://127.0.0.1:8765/v1`. The plugin's
full schema is in `packages/openclaw-plugin/openclaw.plugin.json`; values not
covered here are advanced/developer options.
