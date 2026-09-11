# Configuration

Setup creates `.env` and `configs/benchmark.yaml` without overwriting existing
files. Most users configure only a provider key and model. The canonical field
definitions are:

- sidecar environment: `services/sidecar/src/clawtune_sidecar/config.py`;
- plugin JSON: `packages/clawtune-plugin/openclaw.plugin.json`;
- benchmark YAML loader: `swe_rebench/config.py`;
- public protocol: JSON Schemas under `contracts/`.

## Daily sidecar state

`.env` controls the sidecar. Common settings are:

| Setting | Default | Purpose |
| --- | --- | --- |
| `CLAWTUNE_TRACE_DIR` | `traces` | OpenClaw trace output |
| `CLAWTUNE_STATE_DIR` | invoking user's state directory | Parent of the persistent daily KB |
| `CLAWTUNE_KB_SEED` | `seeds/demo-v1` | Seed used only when creating new daily state |
| `CLAWTUNE_LLM_UPSTREAM_BASE_URL` | DeepSeek API | OpenAI-compatible upstream |
| `CLAWTUNE_TOKEN` | unset | Local sidecar authentication |
| `CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED` | `true` | Fail closed when strict eBPF telemetry is unavailable |
| `CLAWTUNE_RESOURCE_POLL_INTERVAL_MS` | `50` | Resource sampling cadence |
| `CLAWTUNE_PMU_ENABLED` | `true` | Best-effort tool-level PMU counting |

`CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR` overrides the KB directory directly.
Trace output does not select a KB. `CLAWTUNE_TOOL_RESOURCE_FROZEN=true` is for
read-only evaluation/diagnostics; daily and online benchmark learning use
writable state.

For a different provider:

```bash
CLAWTUNE_LLM_UPSTREAM_BASE_URL=https://openrouter.ai/api/v1
CLAWTUNE_LLM_PROXY_EXPOSE_MODEL=your-visible-model
CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL=provider/real-model
```

The proxy normally forwards OpenClaw's authorization header. Use
`CLAWTUNE_LLM_UPSTREAM_API_KEY_OVERRIDE` only when the proxy intentionally
needs a different credential.

CPU/memory histogram edges, KV-TTL settings, PMU limits, cgroup scopes, and
advanced collector options are documented in [call-load prediction](call-load-prediction.md),
[PMU profiling](pmu-profiling.md), and `.env.example`.

## Benchmark configuration

The supported public runner reads `configs/benchmark.yaml`. The setup-created
file is based on `configs/benchmark.example.yaml`:

```yaml
runtime:
  mode: host-openclaw
  kb_frozen: false
llm:
  api_key: "${LLM_API_KEY}"
  api_key_file: ./configs/llm_api_key.txt
  upstream_base_url: https://api.deepseek.com
  model: your-model-name
  openclaw_model_ref: vllm/your-model-name
batch:
  parallelism: 1
  retry_failed: 0
  task_timeout_seconds: 1800
  agent_timeout_seconds: 0
docker:
  pull_policy: missing
  cpus: 4
  memory_limit: 8g
  privileged: true
  cgroupns_mode: host
  cgroup_mount_rw: true
  cgroup_required: true
```

The common runner overrides `runtime.mode` to `host-openclaw`,
`runtime.kb_frozen` to `false`, and `batch.retry_failed` to `0`.
`batch.parallelism` is the default maximum number of in-flight tasks;
`--parallelism N` overrides it, and `1` is serial.

Tool completion handling is asynchronous inside the sidecar. Accepted
observations become available to predictions under the predictor lock and
enqueue persistence on a single writer, which coalesces concurrent updates.
Task completion drains only that runtime's executions/finalizers, so it does
not impose a global KB barrier or delay other tasks. After every producer has
finished, the runner performs one global durability barrier and records the
final committed generation before stopping the sidecar.

The model-key resolution order is the YAML value/environment expansion, the
configured key file, then `LLM_API_KEY` in the root `.env`. The wrapper passes
only named variables through its narrow sudo allow-list; it does not use broad
`sudo -E`.

### Research-only settings

Deep Research Bench also reads:

```yaml
sandbox:
  image: python:3.11-slim
  workdir: /workspace
web_search:
  enabled: true
  provider: tavily
  api_key: "${TAVILY_API_KEY}"
  api_key_file: ./configs/tavily_api_key.txt
```

The older `swe_rebench/config*.yaml` and
`deep_research_bench/config*.yaml` files configure retained internal runners.
They may still be passed explicitly with `--config`, but their concurrency,
output, gate, and frozen-KB fields do not override the unified runner's public
semantics.

### Output and state ownership

| Workflow | KB | Output |
| --- | --- | --- |
| Daily OpenClaw | `$CLAWTUNE_STATE_DIR/kb` or user-state default | `traces/` |
| Online benchmark | `<run>/kb`, initialized from `--seed` | `.runtime/benchmarks/<benchmark>/<run>/` |
| Offline evaluation | trained seed inside experiment; frozen during test | `.runtime/offline/<experiment>/` |

A benchmark run owns `run.json`, `report.json`, `kb/`, `sidecar/`, `traces/`,
and `workspaces/`. Existing output directories are never overwritten. A new
invocation never merges into daily state or another run.

## OpenClaw plugin

Setup enables the plugin, points it at `http://127.0.0.1:8765`, installs the
trusted managed-execution launcher, and enables automatic sidecar startup.
OpenClaw provider traffic should use `http://127.0.0.1:8765/v1`.

The package default for `autoStartSidecar` is `false`; setup changes the
installed configuration to `true` after validating the privileged runtime.
An empty `sidecarCommand` is intentional: the plugin resolves the checkout,
`.venv`, kernel build tree, and sudo command at launch time. `launcherPath` is
different—it is an absolute trusted execution boundary and setup refreshes it
when the checkout moves.

The configured entry includes the lifecycle permission beside `config`:

```json
{
  "plugins": {
    "entries": {
      "clawtune": {
        "hooks": {"allowConversationAccess": true},
        "config": {
          "endpoint": "http://127.0.0.1:8765",
          "autoStartSidecar": true
        }
      }
    }
  }
}
```

For all plugin fields and defaults, use
`packages/clawtune-plugin/openclaw.plugin.json` rather than copying a second
option list into operational docs.

## Repository namespace

The KB repository key is resolved in this order:

1. `CLAWTUNE_REPO_KEY` (benchmark tasks inject this);
2. plugin `repo` or `CLAWTUNE_REPO`;
3. Git remote `origin`, then working-directory basename;
4. sidecar fallback `CLAWTUNE_TOOL_RESOURCE_REPO` (default `openclaw`).

Start a Gateway from the repository it should learn about, or set an explicit
key when one Gateway must be pinned to a namespace.
