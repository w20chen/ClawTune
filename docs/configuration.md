# Configuration

Daily OpenClaw and online benchmarks have separate provider configuration.
Setup creates `.env` and `configs/benchmark.yaml` without overwriting them.
Run commands from the repository root. Relative key-file and runner output
settings resolve against that root, not the YAML file's parent.

## Daily sidecar and plugin

Set sidecar values in root `.env` or the launch environment; existing process
environment values take precedence. Restart the sidecar after changes.

| Setting | Default / purpose |
| --- | --- |
| `CLAWTUNE_TRACE_DIR` | `traces`; trace output, independent of KB ownership |
| `CLAWTUNE_STATE_DIR` | Invoking user's state directory; daily KB is its `kb/` child |
| `CLAWTUNE_KB_SEED` | Bundled `seeds/demo-v1`, used only to initialize new daily state |
| `CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR` | Direct KB-directory override |
| `CLAWTUNE_TOOL_RESOURCE_FROZEN` | `false`; true disables learning |
| `CLAWTUNE_LLM_UPSTREAM_BASE_URL` | `https://api.deepseek.com` |
| `CLAWTUNE_LLM_UPSTREAM_API_KEY_OVERRIDE` | Optional replacement for the forwarded provider key |
| `CLAWTUNE_TOKEN` | Optional local API token; export the same value to OpenClaw/launcher |
| `CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED` | `true`; managed exec fails closed if collection is unavailable |
| `CLAWTUNE_RESOURCE_POLL_INTERVAL_MS` | `50` |
| `CLAWTUNE_PMU_ENABLED` | `true`; hardware counting is best effort |

On Linux the default daily state is `~/.local/state/clawtune`, respecting
`XDG_STATE_HOME` and the invoking user under sudo. `CLAWTUNE_STATE_DIR` overrides
it. Advanced bucket, KV-TTL, PMU, capacity and collector settings are listed in
[.env.example](../.env.example) and the technical guides in the [documentation map](README.md).
The definitive loader is `services/sidecar/src/clawtune_sidecar/config.py`.

For model-name translation, set `CLAWTUNE_LLM_PROXY_EXPOSE_MODEL` and
`CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL`. OpenClaw provider traffic goes to
`http://127.0.0.1:8765/v1`; plugin lifecycle traffic uses the endpoint without
`/v1`. Provider onboarding is shown in [getting started](getting-started.md).

Setup configures managed execution, a trusted absolute launcher path and
`autoStartSidecar: true`. The package default is false; merely linking the
plugin does not configure a privileged runtime. An empty `sidecarCommand`
lets the plugin resolve the checkout/venv/kernel/sudo command at launch time.
`launcherPath` may be empty for automatic resolution, but setup pins its trusted
absolute location. Conversation-hook permission belongs beside `config`:

```json
{"plugins":{"entries":{"clawtune":{"hooks":{"allowConversationAccess":true},"config":{"endpoint":"http://127.0.0.1:8765","autoStartSidecar":true}}}}}
```

This fragment is not a replacement for the complete setup-generated config.
All plugin fields/defaults are in
[openclaw.plugin.json](../packages/clawtune-plugin/openclaw.plugin.json).

## Benchmark YAML

Use [configs/benchmark.example.yaml](../configs/benchmark.example.yaml) as the
template for `configs/benchmark.yaml`. The common runner accepts `--config`;
if the common file is missing, it checks the legacy SWE/DRB `config.yaml`.

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  api_key_file: ./configs/llm_api_key.txt
  upstream_base_url: https://api.deepseek.com
  model: your-model
  openclaw_model_ref: vllm/your-model
batch:
  parallelism: 1
  task_timeout_seconds: 1800
  agent_timeout_seconds: 0
```

Key resolution: nonempty YAML value/environment expansion, exported
`LLM_API_KEY`, configured key file, then `LLM_API_KEY` from root `.env`.
`LLM_API_KEY_FILE` overrides the key-file path. Without a configured path the
legacy loader defaults to `swe_rebench/llm_api_key.txt`; the common template
explicitly selects `configs/llm_api_key.txt`. Key files contain a raw key on
one line and are ignored by Git.

The unified runner always uses `host-openclaw`, writable online KB state and
zero retries. Repository tasks require eBPF; other adapters do not require
repository clause evidence. `batch.parallelism` is honored unless overridden
by CLI. Output paths are owned by the invocation, not legacy YAML output
settings; `--output` selects a new run directory. `runtime.gate_required` and
`batch.continue_on_error` from retained standalone runners do not control the
unified workflow. The loader is `swe_rebench/config.py`.

Keep the common template's Docker/cgroup settings for repository execution.
`docker.platform` or `SWE_REBENCH_DOCKER_PLATFORM` selects architecture; the
public wrapper defaults it to `linux/amd64` on arm64, including research.
The exported environment override takes precedence over YAML; export
`SWE_REBENCH_DOCKER_PLATFORM=linux/arm64` for a native multi-arch research image.
OpenClaw receives
`DOCKER_DEFAULT_PLATFORM`, not the unsupported `sandbox.docker.platform` key.
Terminal tasks use their own Compose settings rather than inheriting all
repository sandbox limits. See [benchmark boundaries](benchmarks.md).

### Research-specific settings

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

`/workspace` is the supported mount; other workdirs are rejected. The key
fields resolve Tavily credentials: environment, configured value, key file,
then root `.env`. `TAVILY_API_KEY_FILE` overrides the file path. Search keys
are task-local. Disabling search writes `enabled: false` to OpenClaw. Other
providers need their own installed OpenClaw integration and credentials;
these Tavily key fields do not configure them.

BFCL web search instead uses exported `SERPAPI_API_KEY`. `BFCL_REPO_PATH` locates
its native package. The wrapper preserves these named variables through sudo;
it does not use broad `sudo -E`. It does not forward arbitrary environment
variables. See the [BFCL setup](benchmarks.md#bfcl) before running it.

## State and namespaces

| Workflow | KB location |
| --- | --- |
| Daily | User state `kb/` |
| Online | `<run>/kb`, initialized from `--seed` |
| Offline | Experiment `seed/`, frozen during testing |

Each owner writes independently; there is no automatic merge. `kb status
--path <kb>` reports committed ownership/generation. Online tasks and offline
queries use `<benchmark>:<group>` namespaces. Daily namespace selection uses
`CLAWTUNE_REPO_KEY`, explicit plugin repo/environment configuration, then Git
origin/workspace inference, with the sidecar fallback when identity is absent.
