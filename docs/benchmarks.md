# Benchmarks and Offline Evaluation

Online runs execute tasks through OpenClaw and collect predictions and measurements. Offline evaluation reads existing traces without model calls. The adapters support resource-prediction research.

## 1. Configure a first run

Complete machine preparation in the [installation guide](getting-started.md). Every
online benchmark has a tracked configuration and task roster, so the normal
benchmark command does not need `--config` or `--dataset`. Setup also creates
`configs/benchmark.yaml`; when present, that Git-ignored file is the local
configuration override shared by all benchmark adapters.

### Tracked defaults

The following files are part of this repository. The bundled rosters are small
smoke/default inputs, not complete upstream evaluation sets.

| Benchmark | Default configuration | Default task list |
| --- | --- | --- |
| SWE-Rebench | `benchmarks/defaults/swe-rebench/config.yaml` | `swe_rebench/tasks.json` |
| SWE-bench Verified | `benchmarks/defaults/swe-bench-verified/config.yaml` | `benchmarks/defaults/swe-bench-verified/tasks.json` |
| Deep Research Bench | `benchmarks/defaults/deep-research-bench/config.yaml` | `deep_research_bench/tasks.json` |
| BFCL | `benchmarks/defaults/bfcl/config.yaml` | `benchmarks/defaults/bfcl/tasks.json` |
| Terminal Bench | `benchmarks/defaults/terminal-bench/config.yaml` | `benchmarks/defaults/terminal-bench/tasks.json` (task files under the same directory) |

When no explicit path is supplied, configuration resolution is:
`--config <file>`, then `configs/benchmark.yaml`, then the tracked benchmark
configuration in the table. Dataset resolution is `--dataset <file-or-directory>`,
then the tracked task list, with the historical sibling `agent-test-bench` lookup
only as a compatibility fallback if the tracked list is unavailable.

Edit `configs/benchmark.yaml` when setup has created it, or copy
`configs/benchmark.example.yaml` to that path in a fresh checkout, keeping the
template's Docker and cgroup settings initially:

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  api_key_file: ./configs/llm_api_key.txt
  upstream_base_url: https://your-provider.example
  model: your-model
  openclaw_model_ref: vllm/your-model
batch:
  parallelism: 1
  # Task budget defaults to 1200 seconds; no timeout configuration is needed.
```

`model` is the upstream model name; `openclaw_model_ref` is its corresponding `vllm/` reference. These settings are independent of daily OpenClaw configuration. Relative credential-file paths resolve against the repository root.

Place the raw provider key on one line in the Git-ignored `configs/llm_api_key.txt` (no quotes and no `LLM_API_KEY=` prefix). With the template configuration, this is sufficient: **you do not need to export `LLM_API_KEY`**. Then validate selection and run one task:

```bash
python3 scripts/clawtune.py benchmark --list
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --sample 1 --parallelism 1 --dry-run
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --sample 1 --parallelism 1
```

For an existing key file elsewhere, set `llm.api_key_file` to its path; `swe_rebench/llm_api_key.txt` is the legacy default when no file is configured. Environment-based credentials remain optional. Do not commit keys in YAML. Resolution order is: nonempty YAML value or environment expansion, exported `LLM_API_KEY`, configured key file, then root `.env`. `LLM_API_KEY_FILE` overrides the file path. If switching from an old environment-based setup to the configured file, clear stale overrides with `unset LLM_API_KEY LLM_API_KEY_FILE`.

Dry-run checks task structure, selection, and initialization data, not image availability, credentials, or live collection.

Live collection requires Linux cgroup v2 with delegated CPU and memory
controllers. Terminal Bench uses each task's Compose environment and honors
`docker.cgroup_required`. BFCL keeps a persistent worker for multi-turn state
and measures native function calls over their call windows; these calls do not
provide shell-subcommand observations or per-call PMU measurements.
Environment memory includes cgroup charges and cache. Missing measurements
remain marked as unavailable in raw output, while training retains approximate
values and fills missing numeric targets with zero.

Per-task output is saved under `.runtime/benchmarks/<benchmark>/<run>/traces/<task-digest>/` (under `--output` when set); [Inspect online output](#4-inspect-online-output) lists every file and the per-benchmark differences. The harness does not stream agent output to the terminal: benchmark runs keep the plugin console quiet and discard agent stdout, so a task prints only a few harness lines while it runs. Use `less <path>` to inspect the per-task files.

## 2. Prepare tasks

Common inputs are a JSON array, JSONL, or a JSON object containing a `tasks`, `instances`, or `data` array. Task IDs must be unique. Datasets and traces are read-only inputs; prepare exports and execution outputs elsewhere.

Setup does not download complete benchmarks. Supply custom inputs with `--dataset`; the tracked smoke rosters are only for initial checks.

### Upstream sources and versions

| Benchmark | Version / source snapshot | Input used by ClawTune |
| --- | --- | --- |
| SWE-Rebench | [Hugging Face, revision `89cdfba`](https://huggingface.co/datasets/nebius/SWE-rebench/tree/89cdfbab4ab1bd8f5a658bb212d1b63624f4f881) | Bundled smoke roster by default; upstream `filtered` tasks with images when supplied as a custom dataset |
| SWE-bench Verified | [Hugging Face, revision `c104f84`](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/tree/c104f840cc67f8b6eec6f759ebc8b2693d585d4a) | Bundled smoke roster by default; Verified `test` split (500 tasks) as a custom dataset |
| Deep Research Bench | [Hugging Face, revision `f7d27cd`](https://huggingface.co/datasets/muset-ai/DeepResearch-Bench-Dataset/tree/f7d27cdd3930dd1eaf67a217821e616cc62e9f8e) | Bundled smoke roster by default; `generated_reports/openai-deepresearch.jsonl` as a custom dataset |
| BFCL | [Gorilla BFCL v4, commit `6ea5797`](https://github.com/ShishirPatil/gorilla/tree/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard) | Bundled `multi_turn_base` smoke entry by default; selected executable categories listed [below](#bfcl) |
| Terminal Bench | [Terminal-Bench 1 repository, commit `d28711d`](https://github.com/harbor-framework/terminal-bench-1/tree/d28711d0da2675d0bb1d56de45ae5df6082438a3/original-tasks) | Bundled smoke task by default; legacy `task.yaml` tasks in `original-tasks` as a custom dataset |

These links identify exact reference snapshots, not automatic version pins or the provenance of every existing run. SWE-Rebench and Research discovery currently fetch the upstream default revision; retain the exported task file and use `--dataset` to repeat a selection. For Git sources, obtain the linked commit before preparing tasks and retain `git rev-parse HEAD` with your experiment inputs. The Terminal directory is a repository task collection, not a pinned `terminal-bench-core` leaderboard release.

Optional **image preparation** reduces startup waiting without changing benchmark execution or configuration:

- Install optional dependencies: `bash scripts/setup/benchmark_cache_dependencies.sh`.
- Prepare a new cache directory: `.venv/bin/python scripts/benchmark_cache.py prepare --directory ~/benchmark-cache --swe-rebench 30 --swe-bench-verified 20 --download-missing`; set each dataset's count with `--<benchmark> N`, supply inputs with `--dataset NAME=PATH`, and add `--build-terminal` to warm Terminal build layers.
- Start in the background: `.venv/bin/python scripts/benchmark_cache.py run --directory ~/benchmark-cache --detach`.
- Check progress: `.venv/bin/python scripts/benchmark_cache.py status --directory ~/benchmark-cache`.

### SWE-Rebench

Supply an ID, `problem_statement`, and `docker_image`. The image must contain the prepared repository at `/testbed`. Provide `repo`, or use an `owner__repo-issue` ID from which it can be inferred:

```json
[{"instance_id":"org__repo-1","repo":"org/repo","problem_statement":"Fix the issue","docker_image":"registry/task:tag"}]
```

The default run uses the tracked `swe_rebench/tasks.json` automatically:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --sample 3 --parallelism 3
```

To use a larger or custom roster, export it with `swe_rebench.discover` and
pass that file explicitly with `--dataset`. Use the dataset-provided image, not
a generic Python image. Startup checks exclusive cgroup and eBPF support.
Individual collection failures are recorded separately from task outcomes.

### SWE-bench Verified

Uses the same repository fields with a separate dataset identity. The default
run uses the tracked smoke roster
`benchmarks/defaults/swe-bench-verified/tasks.json`:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified \
  --sample 1 --parallelism 1
```

For official evaluation, export the pinned upstream records and pass the
resulting file as a custom dataset:

```bash
.venv/bin/python -m pip install datasets
mkdir -p .runtime/datasets
.venv/bin/python -c "from datasets import load_dataset; load_dataset('princeton-nlp/SWE-bench_Verified', revision='c104f840cc67f8b6eec6f759ebc8b2693d585d4a', split='test').to_json('.runtime/datasets/verified.jsonl')"
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified \
  --dataset .runtime/datasets/verified.jsonl \
  --sample 3 --parallelism 3
```

Without an explicit image, the adapter derives the official x86_64 image name from the task ID. ARM hosts require the emulation setup described in the installation guide.

### Deep Research Bench

Supply an ID and nonempty `problem_statement`, `prompt`, or `question`. Optional `topic` or `domain` controls grouping. Reference answers are saved with output, not given to the executing model.

The default run uses the tracked `deep_research_bench/tasks.json` automatically:

```bash
python3 scripts/clawtune.py benchmark --benchmark deep-research-bench \
  --sample 3 --parallelism 3
```

To use a larger upstream selection, install the optional dependencies, discover
into a new file, and pass that file as a custom dataset:

```bash
.venv/bin/python -m pip install huggingface_hub
openclaw plugins install @openclaw/tavily-plugin
.venv/bin/python -m deep_research_bench.discover --source hf \
  --sample 3 --out .runtime/research-tasks.json
export TAVILY_API_KEY="<tavily-key>"
python3 scripts/clawtune.py benchmark --benchmark deep-research-bench \
  --dataset .runtime/research-tasks.json \
  --sample 3 --parallelism 3
```

The template uses `python:3.11-slim` and the required `/workspace` mount. Search uses the [Tavily plugin](https://docs.openclaw.ai/tools/tavily). Credentials can also be stored in `configs/tavily_api_key.txt`. If OpenClaw has a `plugins.allow` list, add `tavily` while preserving other trusted plugins. Inspect the task's `web-search-config.log` to confirm provider setup.

For a key stored elsewhere, use `export TAVILY_API_KEY_FILE=/absolute/path/to/key.txt` instead of exporting the key. OpenClaw's HTTP proxy must use an `http://` or `https://` endpoint, not `socks5://` or `socks5h://`. If the machine can access HTTPS directly, clear incompatible proxy variables in the current shell before running Research:

```bash
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
```

`web_search.enabled: false` disables search. A model can still answer without calling tools; such a task has no learning observations. Verify successful `web_search`/`web_fetch` spans before treating a run as online research. Their local resources belong to the shared OpenClaw host process, not the sandbox or the remote search service.

### BFCL

Prepare the [Gorilla BFCL v4 package](https://github.com/ShishirPatil/gorilla/tree/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard) and its dependencies. If using an existing read-only checkout, copy it before installation so build metadata stays outside the source dataset:

```bash
mkdir -p .runtime/dependencies
cp -R /data/gorilla/berkeley-function-call-leaderboard .runtime/dependencies/bfcl
export BFCL_REPO_PATH="$PWD/.runtime/dependencies/bfcl"
.venv/bin/python -m pip install "$BFCL_REPO_PATH"
```

The default run uses the tracked processed entry list
`benchmarks/defaults/bfcl/tasks.json` (category `multi_turn_base`):

```bash
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --sample 1 --parallelism 1
```

Replace `/data/gorilla` with the actual checkout. Use a fresh copy when changing revisions. The bundled default is `multi_turn_base`; selecting another category without a matching bundled entry falls back to native category loading and needs these dependencies even in dry-run.

To run long-context tasks instead, add `--category multi_turn_long_context` to the benchmark command. Install BFCL dependencies into ClawTune's `.venv`; installing them only in another virtual environment does not make them available to the benchmark wrapper.

Function state persists across turns of one task; tasks are independent. **This is a partial BFCL v4 integration.** The scope below follows the upstream [v4 category definitions](https://github.com/ShishirPatil/gorilla/blob/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard/bfcl_eval/constants/category_mapping.py) and ClawTune's executable-task constraints:

| Category (`--category`) | ClawTune support |
| --- | --- |
| `multi_turn_base` | Supported; default example |
| `multi_turn_long_context` | Supported; uses the long-context backend state |
| `multi_turn_miss_param` | Supported; missing parameters are resolved through the supplied user turns |
| `web_search_base`, `web_search_no_snippet` | Conditional support through BFCL's search backend; requires `SERPAPI_API_KEY` and network access, not the OpenClaw Tavily tool |
| `multi_turn_miss_func` | Unsupported: requires adding missing functions between turns |
| `memory_kv`, `memory_vector`, `memory_rec_sum` | Unsupported: requires prerequisite/dependent-task scheduling |
| Single-turn `simple_*`, `multiple`, `parallel*`, `irrelevance`; `live_*`; `format_sensitivity` | Outside this integration: no general AST-only execution or official format-sensitivity evaluation |

Use a concrete category name, not upstream collection aliases such as `all`, `multi_turn`, or `agentic`. Support here describes the execution adapter; ClawTune does not compute official BFCL scores or claim full-category model validation.

With a custom `--dataset`, provide processed entries containing `id`,
turn-structured `question`, `function` documentation, and `involved_classes`,
optionally `initial_config`. Raw category files missing function documentation
are not equivalent to loader output.

### Terminal Bench

Supports [Terminal-Bench 1 tasks](https://github.com/harbor-framework/terminal-bench-1/tree/d28711d0da2675d0bb1d56de45ae5df6082438a3/original-tasks) defined by `task.yaml` with Compose or a Dockerfile. Tasks defined by `task.toml` (the Harbor format) are unsupported. Obtain the pinned task checkout outside your run outputs, then use its `original-tasks` directory:

```bash
mkdir -p .runtime/datasets
git clone --no-checkout --depth 1 https://github.com/harbor-framework/terminal-bench-1.git .runtime/datasets/terminal-bench-1
git -C .runtime/datasets/terminal-bench-1 fetch --depth 1 origin d28711d0da2675d0bb1d56de45ae5df6082438a3
git -C .runtime/datasets/terminal-bench-1 checkout --detach FETCH_HEAD
git -C .runtime/datasets/terminal-bench-1 rev-parse HEAD
```

The default run uses the tracked smoke list
`benchmarks/defaults/terminal-bench/tasks.json`:

```bash
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --sample 1 --parallelism 1
```

For the pinned upstream task checkout, pass its task directory as a custom
dataset:

```bash
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --dataset .runtime/datasets/terminal-bench-1/original-tasks \
  --sample 3 --parallelism 3
```

If you already have a task checkout, replace `.runtime/datasets/terminal-bench-1/original-tasks` with its directory containing at least three tasks. A single task directory or `task.yaml` is also accepted. In JSON lists, `task_path` resolves relative to the list file, for example `[{"task_path":"tasks/my-task"}]`.

Setup installs a matched Compose/Buildx pair into the invoking user's Docker plugin directory. The wrapper preserves that Docker configuration during elevation. `python3 scripts/clawtune.py check` tests an actual Compose build; a benchmark dry-run only validates task selection.

Tasks are copied into the run before container creation. Compose must provide exactly one `client` container, and host mounts/build contexts must remain within allowed run paths. Each tool invocation uses a fresh shell: use explicit `cd` and files for persistent state. Persistent interactive TTYs are unsupported. Individual telemetry failures do not prevent command execution. See `terminal-logs/<task>/terminal-gate.log` for diagnostics.

Compose startup/build and cleanup logs are written live to `terminal-logs/<task>/compose-up.log` and `compose-down.log` under the run directory. Agent diagnostics for this adapter are `traces/<task>/turn-0-agent-stderr.txt` after the turn finishes. Model output is stored only in `trace.jsonl`; stdout is not duplicated into another content log. `docker.platform` supplies the default service platform; explicit task platforms take precedence.

A Compose build failure fails that case and allows the batch to continue after successful cleanup. A cleanup failure stops the batch.

### PMU results

Terminal and repository shell commands can supply tool-level PMU profiles; BFCL functions and native research tools have no per-call PMU measurement. Compound commands produce one execution-tree profile, not per-clause counters. Collector quality is separate from the available-or-zero training policy; imputed labels are not measured hardware counts. Its nine PMU targets include LLC accesses and misses per counter running second; see [definitions and units](tool-profile.md#pmu-resourcespmu). On ARM hosts running amd64 images, counters include QEMU overhead. Availability does not establish prediction accuracy.

## 3. Selection, concurrency, and resume

`--sample N` selects the first N tasks, not a random sample. Processing order is repository filtering, explicit IDs, skip, then sample:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset /data/tasks.jsonl --skip 2 --sample 4 --parallelism 2 \
  --output .runtime/my-run
```

`--output` must name a new directory outside datasets and immutable priors. The
default configuration and dataset can be overridden independently. For example,
use `--config ./configs/my-benchmark.yaml` for a custom YAML file and/or
`--dataset ./data/my-tasks.jsonl` for a custom task list. Paths are resolved from
the repository root in the documented commands. Initialization and state
ownership are described once in [Output and persistent state](getting-started.md#3-output-and-persistent-state).

For background runs, configure noninteractive sudo (`sudo -n true`) before launching with `nohup` or a service manager. Use a separate output directory for each run; `--parallelism` applies per run.

| Option | Meaning |
| --- | --- |
| `--repo owner/repo` | Filter repository tasks |
| `--instance-ids id1,id2` | Select tasks in the supplied order |
| `--parallelism N` | Maximum in-flight tasks; otherwise use YAML |
| `--task-timeout-seconds N` | Whole-task timeout |

All five benchmarks share a **1,200-second** default task deadline covering setup, agent turns, tool execution, and normal result collection. Each parallel task has its own clock. CLI `--task-timeout-seconds` overrides `batch.task_timeout_seconds`; `--dry-run` displays the resolved budget. `0` disables the harness deadline; negative, fractional, and boolean values are rejected. The legacy `agent_timeout_seconds` does not create a second deadline.

OpenClaw and model-supplied limits remain effective. The BFCL/Terminal bridge adds no separate call timer; Terminal's native `max_agent_timeout_sec` is metadata only. These runs therefore do not reproduce Terminal's native timing/scoring conditions.

A harness timeout records exit code `124` and `scope: task`; the first timeout reason is preserved. Cleanup and KB drain have separate bounded grace periods, so wall time can exceed the task budget. Cleanup failure stops dispatch of new tasks. Prepare slow image downloads/builds before starting the run.

Resume with:

```bash
python3 scripts/clawtune.py benchmark --resume .runtime/my-run
```

Resume requires the original configuration and prior; repeat `--seed` if it was customized. Saved task selection and parallelism are reused. Recorded failures are not retried. Ctrl+C stops tasks and attempts cleanup. A run with interrupted in-flight tasks or incompletely saved final state cannot resume; start a new run.

Resume preserves saved learning state. If a legacy run reports incompatible learning data, start a new run from the original traces or task inputs rather than reusing incomplete state.

## 4. Inspect online output

Defaults are under `.runtime/benchmarks/<benchmark>/<run>/` (or `--output`). All five benchmarks launched by `scripts/clawtune.py benchmark` share this layout; only the per-task files differ, as noted below.

Agent progress is file-based: follow `trace.jsonl` and agent stderr while `run.json` tracks task status. The console prints startup diagnostics and a result line per task; agent stdout is not duplicated.

Run-level layout (`<task-digest>` identifies one task):

| Location | Semantics |
| --- | --- |
| `run.json` | Live run manifest, updated from `preparing` through `running` to the final state. Validated by `contracts/benchmark-run.schema.json`: `status`, `task_order`, `active_tasks`, `parallelism`, `config_sha256`, `kb_flush_complete`, `observation_issues`, `error`, and one `results` row per finished task (`exit_code`, `error`, `kb_generation_before/after`, `learning_status` recording whether shared-KB progress was observed, plus artifact summaries for repository tasks). |
| `report.json` | The same manifest written when the run ends, on every exit path (completed, failed, or interrupted). Read this for the final state; `status: completed` requires `kb_flush_complete: true`. |
| `traces/<task-digest>/` | Per-task files (next table). |
| `kb/` | The run's shared learned statistics; `state.json` records the committed generation. Operational learning state, not a trace export. |
| `sidecar/` | Local service logs and working state. Task traces are in `traces/<task-digest>/trace.jsonl`. |
| `runtime-assets/` | Generated configuration and runtime dependencies used by task environments. |
| `workspaces/<task-digest>/` | Task working directory. |
| `runtime/<task-digest>/openclaw-home/` | Agent session state. Deep Research Bench keeps its `openclaw-home/` inside the trace directory. |
| `terminal-environments/<task-digest>/`, `terminal-logs/<task-digest>/` | Terminal Bench Compose environments and logs. |

Per-task files in `traces/<task-digest>/` (BFCL and Terminal Bench tasks are "bridged": one agent process per conversation turn):

| File | Written for | Semantics |
| --- | --- | --- |
| `trace.jsonl` | all | Canonical task trace (all sessions and turns): LLM/tool spans, predictions, resource timelines, PMU and eBPF data. The only experiment trace export. |
| `dataset-task.json` | all | Task identity (`benchmark`, `instance_id`, `repo`, `category`) written when the result is recorded; offline evaluation reads it for benchmark identity. |
| `agent_prompt.txt` | repository, research | Exact prompt handed to the agent |
| `turn-<n>.txt` | BFCL, Terminal Bench | Per-turn prompt for the bridged executor |
| `agent-stderr.txt` | repository, research | Full stderr of the host `openclaw agent` process, written live. Host runs discard agent stdout, so no `agent-stdout.txt` is produced; canonical agent content is in `trace.jsonl`. |
| `turn-<n>-agent-stderr.txt` | BFCL, Terminal Bench | The same stderr stream, preserved once per turn |
| `task-timeout.json` | all (on timeout) | The first timeout record (`scope`, `configured_seconds`, `message`); later phases do not rewrite it |
| `observation-issues.jsonl` | all (on issues) | Monitoring, drain, or KB issues that did not fail the task; also mirrored into the matching `results` row |
| `runtime-finalization.json` | all (on timeout/cancellation) | Executions finalized only after confirmed agent and sandbox shutdown; measurements without a confirmed tool exit stay incomplete |
| `phase3.log` | all | `openclaw onboard`, plugin install, and config patch log for the task |
| `plugin-build.log` | first task when rebuilt | npm build log for the ClawTune plugin |
| `agent-cwd.txt`, `task_manifest.json` | repository, research | Agent working directory and task/model/image metadata |
| `repo_status.txt`, `model.patch` | repository | `git status`/`diff --stat` and the workspace diff against the base commit |
| `result_summary.json` | repository, research | Exit code, `has_patch`/`patch_bytes` (repository), runtime mode, and error |
| `reference_answer.txt` | research (when provided) | Held-out reference answer for later offline grading; never shown to the model |
| `web-search-config.log` | research | Result of the search provider/key configuration patch |
| `host_openclaw_error.txt` | repository | Python traceback when the runner failed the task outside the agent |
| `drb_host_error.txt` | research | Equivalent traceback for research tasks |
| `tool_resource_preflight_host.json` | repository (always); Terminal Bench when `ebpf_required` | Host eBPF/clause-telemetry preflight result |
| `launcher-preflight.log` | repository, research | `clawtune-launch diagnose` output from the sandbox |
| `sandbox-runtime-preflight.log` | repository (Python tasks) | Testbed python/pip PATH preflight in the sandbox |
| `sandbox-image-build.log` | repository | Log of tagging the task image as the runtime-private sandbox image |
| `sandbox-container-cleanup.log` | repository, research | Stale sandbox container cleanup before and after the agent run |
| `sandbox_scope.json`, `sandbox_scope_discovery_last_error.txt` | repository, research | cgroup scope discovery for the task sandbox |
| `tool-bridge.json` | BFCL, Terminal Bench | Tool-bridge manifest for the task; exists only while the task runs |

While a run is in progress, follow it without waiting for the result line:

```bash
RUN=.runtime/benchmarks/swe-rebench/<run>
tail -F "$RUN/traces/<task-digest>/trace.jsonl"       # spans appear as the agent works
tail -F "$RUN/traces/<task-digest>/agent-stderr.txt"  # full agent stderr (repository/research)
tail -F "$RUN/sidecar/sidecar-stderr.txt"             # shared service log
docker ps --filter name=clawtune-srb                  # sandbox containers of active repository/research tasks
```

Check run `status`, each result's `error`/`exit_code`, and `kb_flush_complete`. A completed run requires persisted KB state. `observation_issues` and collector logs report monitoring failures separately; unavailable targets can be valid, including empty cold-start history. `resource_summary.prediction_models` records availability for `tool`, `trie`, and `lattice`, not prediction accuracy.

KB updates remain available to subsequent online queries before they are saved.
The run saves shared knowledge at its final flush; task-level on-disk generation
counters can remain unchanged despite active learning.

Each task has one `trace.jsonl` containing all turns, full captured LLM messages, and inlined telemetry. Join `execution_telemetry` by `execution_id`; do not count its compact and full views twice. `incomplete_span`, `llm_proxy_unmatched`, and aborted profiles retain incomplete evidence, not successful observations. See the [trace reference](tool-profile.md).

Timeout finalization follows confirmed producer shutdown. Incomplete tools retain their available observations for training and their interruption markers; unavailable numeric targets use the documented zero-fill policy. Abrupt failures can lose unacknowledged events; `trace_flushed: false` or `kb_flush_complete: false` means persistence is incomplete. A successful exit without tool calls or measurements does not validate collection.

## 5. Fixed-trace offline evaluation

`scripts/clawtune.py offline` evaluates the three base prediction models and
subcommand composition; see the
[prediction reference](tool-profile.md#prediction-span_startprediction).

Offline evaluation freezes the knowledge base and available command rules.
It does not generate rules or call a model provider. Preserve the rule files,
tool versions, and original traces with an evaluation to make it reproducible.

### Execution replay

`swe_rebench.runner replay` accepts the existing v6 span format and v5 action JSONL with recorded `llm_call` responses and `tool_exec` arguments. Supply one recorded attempt or a flat directory; `--task-id` selects the matching v5 task. Use the original task image and a Linux host with Docker, OpenClaw and the normal telemetry prerequisites.

Run from the repository root with its installed Python environment and root telemetry privileges:

```sh
PYTHON="$PWD/.venv/bin/python"
export PYTHONPATH="$PWD/services/sidecar/src:$PWD"
export CLAWTUNE_PMU_ENABLED=true
sudo -E env "PATH=$PATH" "$PYTHON" -m swe_rebench.runner replay --config /path/to/config.yaml \
  --trace /data/recorded-traces --tasks /data/tasks.json \
  --task-id <instance-id> --timing none
```

Replace the paths and task ID with your inputs. Runtime assets are built automatically when needed; `--prepare` forces rebuilding them. The task roster must identify the original image. For x86 task images on Kunpeng/ARM, install and check the [QEMU setup](getting-started.md#arm-hosts), and configure `docker.platform: linux/amd64`. Use `runtime.mode: host-openclaw` and `runtime.kb_frozen: false` for a new writable run with the bundled seed. A working Docker daemon, OpenClaw executable on `PATH`, cgroup delegation, eBPF dependencies and PMU access are prerequisites; the command reports preflight failures instead of bypassing them.

V5 replay adapts `exec`/`terminal_exec`, `read_file`, `write_file`, `edit_file`, `list_dir`, and local `message` output. File operations run inside the task sandbox; `/testbed` aliases the writable workspace and shell calls honor their recorded working directory. Other v5 tools are rejected before execution. Recorded model responses and requested arguments are required; changing a version number alone does not convert a trace.

V5 tool batches execute in recorded request order. `--timing exact` preserves recorded model delays; `--timing none` removes them. Replay executes tools again and collects new measurements; it does not reuse historical resource values. Adapted tool batches can change the number of model turns shown in `replay_manifest.json`.

Use `--seed /path/to/seed` to choose an initialization bundle; the default is `seeds/bootstrap-v2`. With `runtime.kb_frozen: true`, replay collects measurements without updating the knowledge base and requires compatible saved models. Use `runtime.kb_frozen: false` for a new learning run. The source bundle remains unchanged. With Docker/QEMU on ARM, PMU counts describe host execution of the emulated workload, not guest x86 instructions.

Timeouts and nonzero exits do not by themselves invalidate PMU measurements. Interpret counter quality and observation windows using the [PMU field reference](tool-profile.md#pmu-resourcespmu); a reliable counter window does not imply that a task completed successfully.

### Offline trace evaluation

Install the [development dependencies](getting-started.md#5-development-checks). No provider key, Docker execution, or running collector is needed. Use the JSONL execution traces produced by ClawTune, retaining their metadata and adjacent task identity files, **not online task lists**.

For an online run, pass its task trace subtree:

```bash
python3 scripts/clawtune.py offline --dataset /path/to/run/traces \
  --rss-unit MiB --train-fraction 0.8 --seed 42 \
  --output .runtime/offline-study
```

Supply missing benchmark identity for historical traces:

```bash
python3 scripts/clawtune.py offline --dataset /data/fixed-traces \
  --benchmark swe-rebench --rss-unit MiB
```

Choose `MB` or `MiB` according to original RSS units; explicit bytes retain their units. Tasks need `instance_id` or `task_id`. Adjacent `dataset-task.json` can supply identity. SWE datasets require `repo` or an ID from which it can be inferred.

Offline grouping uses `repo`, otherwise `category`, otherwise `dataset`. Observations receive `repo="<benchmark>:<group>"`. Terminal traces with neither repo nor category therefore share `terminal-bench:dataset`, not a per-task repository. LatticeKB treats it as an optional context feature; command features still distinguish workloads, and nodes without repo can share evidence across contexts. Task IDs keep all attempts on one side of the split.

The split methodology is in the [technical report](technical-report.md#6-learning-initialization-and-evaluation). `--train-fraction` defaults to 0.8 and must lie strictly between 0 and 1. Each group assigns `max(1, floor(fraction*N))` tasks to training and the rest to testing. Assignments are reused by task roster, seed, and fraction. The registry defaults to `.runtime/offline/splits/`; use `--split-cache-dir` to relocate it.

For a cold-start seed evaluated on unseen repositories, add `--split-by repository`. This assigns the requested fraction of repositories to training and all remaining repositories to testing; all tasks and attempts from a repository stay together. It requires repository identities and at least two repositories per benchmark. The split registry is separate from the default `--split-by task`, which measures new tasks in known repositories. Keep these two results separate: their training data and evaluation populations differ. The generated `seed/` contains only training data and remains frozen throughout testing.

The evaluation seed uses benchmark-prefixed repository names. To export a runtime seed from a single benchmark's fresh trace-v6 dataset, use the evaluation's repository split:

```bash
python3 scripts/build_replayed_seed.py --dataset /data/fresh-traces \
  --split /data/evaluation/split.json --output /data/new-runtime-seed
```

This exports only training traces, checks their recorded hashes, and uses
ordinary repository names for runtime matching. The output directory must be new.
For deployment, `--include-test` also imports the test partition. That larger
bundle must not be evaluated as though those tasks were held out. The bundled
`bootstrap-v2` already includes its former test partition; use new independent
traces to evaluate it, or use the training-only bundle for the original split.

| Output | Purpose |
| --- | --- |
| `split.json` | Task assignments and input hashes |
| `seed/` | Statistics constructed only from training data |
| `predictions.jsonl` | ToolKB test predictions and training-policy labels, including PMU |
| `model-predictions.jsonl` | ToolKB, TrieKB, and LatticeKB predictions with a `model` field |
| `report.json`, `report.md` | Availability, errors, baselines, and exclusions |

Mixed input is trained and evaluated separately per benchmark, with an aggregate report. Check train/test counts and `test_updates: 0` before interpreting metrics. All-singleton groups can leave no test set.

Offline evaluation reports only `exec` and `terminal_exec` calls. Non-shell calls are excluded from prediction outputs, coverage denominators, and all reported model metrics. Task splitting and training ingestion are unchanged; a test set containing no shell calls produces no scored results. `tools/summarize_offline_buckets.py` applies the same reporting scope when reading older mixed-tool results.

Each benchmark's `report.json.models` reports `tool`, `trie`, and `lattice` separately. The existing top-level `metrics` and `repositories` describe ToolKB. Per-target `availability` counts all uncensored queries (`queries`), queries with effective labels (`labeled`), available predictions (`predicted`), and labeled predictions that can be scored (`scored`). Effective labels can include explicit zero imputation, so coverage is not a count of direct measurements. Partial executions remain training inputs but are excluded from completed-task MAE/WAPE.

`availability.unavailable_reasons` counts failed predictions over all queries, including those without labels. Prediction rows contain `task` and `call_index` for matching the same call across models on an unchanged dataset.
Each model also reports labeled metrics and prediction availability by repository in `models.<model>.repositories`. Repositories without labels still have query and prediction counts. Inspect these alongside pooled metrics when repository sizes differ substantially; a large repository can dominate the pooled result.

Add `--lattice-variants` to report `lattice_loso` and `lattice_max_cardinality`. Add the independent `--lattice-oracle` flag to report `lattice_oracle`; use both flags to evaluate all variants. They use the same frozen training data and subcommand composition as the default `lattice` shrinkage method. The optional-feature limit defaults to six and can be changed as described below; complete feature sets are always retained.

`lattice_oracle` is an offline diagnostic for duration only: it uses the actual test-call duration to exhaustively choose the candidate-node combination minimizing absolute error of the composed call p50. It retains execution-identity constraints and exact-own-sample preference. Missing duration labels produce unavailable predictions. It never updates training data and is not an online predictor. Its p90 and bucket probabilities come from the chosen historical distributions; it does not optimize bucket accuracy. Multi-clause search cost grows with the product of candidate counts. The existing 512-candidate computation guard applies to shrinkage scoring, not to oracle enumeration, so compare coverage as well as paired errors.

Bucket predictions choose the largest probability, breaking ties toward the lower bucket. Reported bucket accuracy and recall use available predictions only; consult coverage alongside them. Count unavailable predictions as misses separately when measuring recall across all labeled calls.

Recorded execution-interval CPU deltas provide CPU-time measurements; a
historical cumulative process counter alone does not. Shell workload duration
uses the retained subcommand intervals, separately from tool-call elapsed time.
Missing numeric training targets are filled with zero, and available approximate
values are retained. Environment-memory measurements keep their own definition;
RSS is not a substitute. `--rss-unit` applies to v5 RSS; v6 compact RSS is already
decimal MB. When judging resource accuracy, inspect measured, approximate, and
imputed counts separately: a zero-filled test label is not measurement ground truth.

Rebuild seeds from the original traces after changing label ingestion rules. Existing snapshots retain previously imported values and cannot recover missing measurement evidence.

The offline `--kb-max-optional-features <n>` option sets the fixed combination limit for Lattice (default 6). It is recorded in the Lattice snapshot; the evaluator does not automatically reduce it. Compare runs at the same limit. Use `CLAWTUNE_KB_MAX_OPTIONAL_FEATURES=<n>` when opening the resulting seed through the sidecar.
