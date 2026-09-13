# Benchmarks and Offline Evaluation

Online runs execute tasks through OpenClaw and collect predictions and measurements. Offline evaluation reads existing traces without model calls. The adapters support resource-prediction research; **they do not run official answer graders**. `official_score` is `null`, and a completed task is not necessarily solved correctly.

## 1. Configure a first run

Complete machine preparation in the [installation guide](getting-started.md). Setup creates `configs/benchmark.yaml`; copy the [configuration template](../configs/benchmark.example.yaml) to create another configuration.

Run the commands below from the ClawTune repository root on the configured Linux machine. The online wrapper selects the collector's `.venv` and requests sudo when needed. Each benchmark section includes a **3-task, parallelism-3** example. Start with `--sample 1 --parallelism 1` if machine resources are limited.

Edit the model settings, keeping the template's Docker and cgroup settings initially:

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  api_key_file: ./configs/llm_api_key.txt
  upstream_base_url: https://your-provider.example
  model: your-model
  openclaw_model_ref: vllm/your-model
batch:
  parallelism: 1
  task_timeout_seconds: 1800
  agent_timeout_seconds: 0
```

`model` is the upstream model name; `openclaw_model_ref` is its corresponding `vllm/` reference. These settings are independent of daily OpenClaw configuration. Relative credential-file paths resolve against the repository root.

Place the raw provider key on one line in the Git-ignored `configs/llm_api_key.txt` (no quotes and no `LLM_API_KEY=` prefix). With the template configuration, this is sufficient: **you do not need to export `LLM_API_KEY`**. Then validate selection and run one task:

```bash
python3 scripts/clawtune.py benchmark --list
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --config configs/benchmark.yaml --sample 1 --parallelism 1 --dry-run
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --config configs/benchmark.yaml --sample 1 --parallelism 1
```

For an existing key file elsewhere, set `llm.api_key_file` to its path; `swe_rebench/llm_api_key.txt` is the legacy default when no file is configured. Environment-based credentials remain optional. Do not commit keys in YAML. Resolution order is: nonempty YAML value or environment expansion, exported `LLM_API_KEY`, configured key file, then root `.env`. `LLM_API_KEY_FILE` overrides the file path. If switching from an old environment-based setup to the configured file, clear stale overrides with `unset LLM_API_KEY LLM_API_KEY_FILE`.

Defaults use external task lists when available; SWE and research otherwise use bundled smoke inputs. Use explicit dataset paths for experiments. Dry-run checks task structure, selection, and initialization data, not image availability, credentials, or live collection.

Full predictions and agent output are saved in `.runtime/benchmarks/<benchmark>/<run>/traces/<task>/agent-stdout.txt`; errors are in `agent-stderr.txt` (under `--output` when set). Use `less <path>` to inspect them. The live agent stream shows only baseline time buckets: `mode` denotes the most probable bucket (ties retained), `p90` denotes the bucket containing that estimate; clause results remain separate.

## 2. Prepare tasks

Common inputs are a JSON array, JSONL, or a JSON object containing a `tasks`, `instances`, or `data` array. Task IDs must be unique. Datasets and traces are read-only inputs; prepare exports and execution outputs elsewhere.

The external root is `$AGENT_TEST_BENCH_ROOT`, defaulting to the sibling `../agent-test-bench`:

| Benchmark | Default lookup relative to the external root |
| --- | --- |
| SWE-Rebench | `data/swe-rebench/tasks.json`, then bundled `swe_rebench/tasks.json` |
| SWE-bench Verified | `data/swe-bench-verified/tasks.json` or `data/swebench_verified/tasks.json` |
| Deep Research Bench | `data/deep-research-bench/tasks.json`, then bundled `deep_research_bench/tasks.json` |
| BFCL | Category loader in a separately installed BFCL package |
| Terminal Bench | `data/terminal-bench/tasks.json` or `data/terminal-bench/tasks/` |

These are lookup conventions. Setup does not download complete benchmarks.

### Upstream sources and versions

| Benchmark | Version / source snapshot | Input used by ClawTune |
| --- | --- | --- |
| SWE-Rebench | [Hugging Face, revision `89cdfba`](https://huggingface.co/datasets/nebius/SWE-rebench/tree/89cdfbab4ab1bd8f5a658bb212d1b63624f4f881) | `nebius/SWE-rebench`, `filtered` split; dataset-provided task images |
| SWE-bench Verified | [Hugging Face, revision `c104f84`](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/tree/c104f840cc67f8b6eec6f759ebc8b2693d585d4a) | Verified edition, `test` split (500 tasks) |
| Deep Research Bench | [Hugging Face, revision `f7d27cd`](https://huggingface.co/datasets/muset-ai/DeepResearch-Bench-Dataset/tree/f7d27cdd3930dd1eaf67a217821e616cc62e9f8e) | `generated_reports/openai-deepresearch.jsonl`; questions are agent inputs, reports are references only |
| BFCL | [Gorilla BFCL v4, commit `6ea5797`](https://github.com/ShishirPatil/gorilla/tree/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard) | Selected executable categories listed [below](#bfcl), not the complete BFCL suite |
| Terminal Bench | [Terminal-Bench 1 repository, commit `d28711d`](https://github.com/harbor-framework/terminal-bench-1/tree/d28711d0da2675d0bb1d56de45ae5df6082438a3/original-tasks) | Legacy `task.yaml` tasks in `original-tasks`; not Terminal-Bench 2.0 / Harbor `task.toml` |

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

Prepare an input list and run:

```bash
.venv/bin/python -m pip install datasets
.venv/bin/python -m swe_rebench.discover --sample 3 --out .runtime/swe-tasks.json
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --config configs/benchmark.yaml --dataset .runtime/swe-tasks.json \
  --sample 3 --parallelism 3 --dry-run
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --config configs/benchmark.yaml --dataset .runtime/swe-tasks.json \
  --sample 3 --parallelism 3
```

Discovery first attempts the external task source, then Hugging Face. Use the dataset-provided image, not a generic Python image. Execution requires an exclusive cgroup and valid clause-level eBPF measurements.

### SWE-bench Verified

Uses the same repository fields with a separate dataset identity. Export upstream records and run:

```bash
.venv/bin/python -m pip install datasets
mkdir -p .runtime/datasets
.venv/bin/python -c "from datasets import load_dataset; load_dataset('princeton-nlp/SWE-bench_Verified', revision='c104f840cc67f8b6eec6f759ebc8b2693d585d4a', split='test').to_json('.runtime/datasets/verified.jsonl')"
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified \
  --config configs/benchmark.yaml --dataset .runtime/datasets/verified.jsonl \
  --sample 3 --parallelism 3 --dry-run
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified \
  --config configs/benchmark.yaml --dataset .runtime/datasets/verified.jsonl \
  --sample 3 --parallelism 3
```

Without an explicit image, the adapter derives the official x86_64 image name from the task ID. ARM hosts require the emulation setup described in the installation guide.

### Deep Research Bench

Supply an ID and nonempty `problem_statement`, `prompt`, or `question`. Optional `topic` or `domain` controls grouping. Reference answers are saved with output, not given to the executing model.

```bash
.venv/bin/python -m pip install huggingface_hub
openclaw plugins install @openclaw/tavily-plugin
.venv/bin/python -m deep_research_bench.discover --source hf \
  --sample 3 --out .runtime/research-tasks.json
export TAVILY_API_KEY="<tavily-key>"
python3 scripts/clawtune.py benchmark --benchmark deep-research-bench \
  --config configs/benchmark.yaml --dataset .runtime/research-tasks.json \
  --sample 3 --parallelism 3 --dry-run
python3 scripts/clawtune.py benchmark --benchmark deep-research-bench \
  --config configs/benchmark.yaml --dataset .runtime/research-tasks.json \
  --sample 3 --parallelism 3
```

The template uses `python:3.11-slim` and the required `/workspace` mount. Search uses the [Tavily plugin](https://docs.openclaw.ai/tools/tavily). Credentials can also be stored in `configs/tavily_api_key.txt`. If OpenClaw has a `plugins.allow` list, add `tavily` while preserving other trusted plugins. Inspect the task's `web-search-config.log` to confirm provider setup.

For a key stored elsewhere, use `export TAVILY_API_KEY_FILE=/absolute/path/to/key.txt` instead of exporting the key. OpenClaw's HTTP proxy must use an `http://` or `https://` endpoint, not `socks5://` or `socks5h://`. If the machine can access HTTPS directly, clear incompatible proxy variables in the current shell before running Research:

```bash
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
```

`web_search.enabled: false` disables search. A model can still answer without calling tools; such a task fails the learning-observation check. Verify successful `web_search`/`web_fetch` spans before treating a run as online research. Their local resources belong to the shared OpenClaw host process, not the sandbox or the remote search service.

### BFCL

Prepare the [Gorilla BFCL v4 package](https://github.com/ShishirPatil/gorilla/tree/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard) and its dependencies. If using an existing read-only checkout, copy it before installation so build metadata stays outside the source dataset:

```bash
mkdir -p .runtime/dependencies
cp -R /data/gorilla/berkeley-function-call-leaderboard .runtime/dependencies/bfcl
export BFCL_REPO_PATH="$PWD/.runtime/dependencies/bfcl"
.venv/bin/python -m pip install "$BFCL_REPO_PATH"
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --config configs/benchmark.yaml --category multi_turn_base \
  --sample 3 --parallelism 3 --dry-run
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --config configs/benchmark.yaml --category multi_turn_base \
  --sample 3 --parallelism 3
```

Replace `/data/gorilla` with the actual checkout. Use a fresh copy when changing revisions. Native category loading needs these dependencies even in dry-run.

To run long-context tasks instead, use `--category multi_turn_long_context` in both commands. Install BFCL dependencies into ClawTune's `.venv`; installing them only in another virtual environment does not make them available to the benchmark wrapper.

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

With `--dataset`, provide processed entries containing `id`, turn-structured `question`, `function` documentation, and `involved_classes`, optionally `initial_config`. Raw category files missing function documentation are not equivalent to loader output.

### Terminal Bench

Supports [Terminal-Bench 1 tasks](https://github.com/harbor-framework/terminal-bench-1/tree/d28711d0da2675d0bb1d56de45ae5df6082438a3/original-tasks) defined by `task.yaml` with Compose or a Dockerfile. Tasks defined by `task.toml` (the Harbor format) are unsupported. Obtain a checkout containing `task.yaml` files and use its actual task directory:

```bash
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --config configs/benchmark.yaml --dataset /data/terminal-bench/original-tasks \
  --sample 3 --parallelism 3 --dry-run
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --config configs/benchmark.yaml --dataset /data/terminal-bench/original-tasks \
  --sample 3 --parallelism 3
```

Replace `/data/terminal-bench/original-tasks` with your task directory containing at least three tasks. A single task directory or `task.yaml` is also accepted. In JSON lists, `task_path` resolves relative to the list file, for example `[{"task_path":"tasks/my-task"}]`.

Setup installs a matched Compose/Buildx pair into the invoking user's Docker plugin directory. The wrapper preserves that Docker configuration during elevation. `python3 scripts/clawtune.py check` tests an actual Compose build; a benchmark dry-run only validates task selection.

Tasks are copied into the run before container creation. Compose must provide exactly one `client` container, and host mounts/build contexts must remain within allowed run paths. Each tool invocation uses a fresh shell: use explicit `cd` and files for persistent state. Persistent interactive TTYs are unsupported. Terminal execution requires working telemetry; if it is unavailable, the command will not run. See `terminal-logs/<task>/terminal-gate.log` for diagnostics.

Compose startup/build and cleanup logs are written live to `terminal-logs/<task>/compose-up.log` and `compose-down.log` under the run directory. Agent diagnostics for this adapter are `traces/<task>/turn-0-agent-stderr.txt` after the turn finishes. Model output is stored only in `trace.jsonl`; stdout is not duplicated into another content log. `docker.platform` supplies the default service platform; explicit task platforms take precedence.

A Compose build failure fails that case and allows the batch to continue after successful cleanup. A cleanup failure stops the batch.

### PMU results

PMU data is supported for Terminal commands and shell commands in SWE-Rebench, SWE-Bench Verified, and Deep Research. BFCL functions and non-shell research tools currently have no per-call PMU data; missing values are not zero.

All PMU microarchitecture metrics are **tool-level, not clause-level**: for a compound command such as `pip install … && pytest …`, counts and derived metrics cover the whole execution tree, not each clause separately.

The standard Research workflow uses native web tools, not shell commands. Shared-process measurements describe local runtime activity, not an individual function's exclusive CPU/memory. Exclude `partial` and zero-overlap resource samples from CPU/memory labels; valid call duration and independently eligible PMU can still be used. Use a new run/KB, or rebuild from raw traces, when applying updated collection-quality rules; resuming an old KB does not clean previously learned labels.

Only profiles with `coverage.eligible_for_kb=true` are used for learning. Predictions marked `calibration=unvalidated` have not been validated for accuracy. When running amd64 images on arm64, counters include QEMU overhead.

For attributed exec calls, `resources.pmu.events.llc_read_accesses.raw_count` and `llc_read_misses.raw_count` contain the execution tree's LLC read counts. `resources.pmu.derived.llc_read_accesses_per_cpu_second` and `llc_read_misses_per_cpu_second` divide those counts by the corresponding event's `time_running_ns / 1e9`. This measures LLC read intensity per monitored **on-CPU second**, including inherited threads/processes; it excludes blocked time, and parallel threads contribute their accumulated CPU time. It is not wall-time throughput, total memory-access frequency, or DRAM bandwidth. These rates reuse the four existing PMU events with no added polling or hardware counters, and are null for partial/multiplexed/unavailable profiles. Shared-process tools have no exclusive rates. The rates are monitoring outputs; they are not additional prediction targets.

## 3. Selection, concurrency, and resume

`--sample N` selects the first N tasks, not a random sample. Processing order is repository filtering, explicit IDs, skip, then sample:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset /data/tasks.jsonl --skip 2 --sample 4 --parallelism 2 \
  --task-timeout-seconds 1800 --output .runtime/my-run
```

`--output` must name a new directory outside datasets and immutable priors. `--config <file>` selects another configuration. Initialization and state ownership are described once in [Output and persistent state](getting-started.md#3-output-and-persistent-state).

| Option | Meaning |
| --- | --- |
| `--repo owner/repo` | Filter repository tasks |
| `--instance-ids id1,id2` | Select tasks in the supplied order |
| `--parallelism N` | Maximum in-flight tasks; otherwise use YAML |
| `--task-timeout-seconds N` | Whole-task timeout |
| `--agent-timeout-seconds N` | Agent timeout |

A timeout value of 0 disables that layer. Terminal also enforces its task's `max_agent_timeout_sec` and a 300-second shell-call limit; disabling a CLI timeout does not disable the native task budget.

Resume with:

```bash
python3 scripts/clawtune.py benchmark --resume .runtime/my-run \
  --config configs/benchmark.yaml
```

Resume requires the original configuration and prior; repeat `--seed` if it was customized. Saved task selection and parallelism are reused. Recorded failures are not retried. Ctrl+C stops tasks and attempts cleanup. A run with interrupted in-flight tasks or incompletely saved final state cannot resume; start a new run.

## 4. Inspect online output

Defaults are under `.runtime/benchmarks/<benchmark>/<run>/`:

| Location | Purpose |
| --- | --- |
| `run.json`, `report.json` | Task status, errors, and execution summary |
| `traces/<task-digest>/trace.jsonl` | Canonical task trace: LLM/tool spans, predictions, resource timelines, PMU and eBPF data |
| `runtime/<task-digest>/openclaw-home/` | OpenClaw session state required for runtime operation; not an experiment trace export |
| `kb/` | Learned statistics for the run |
| `sidecar/` | Shared local-service logs |
| `workspaces/` | Task workspaces |

Check `status`, each result's `error` and `exit_code`, and `kb_flush_complete: true`, which indicates completed persistence.

Each task writes directly to one `trace.jsonl`, including all sessions and turns. No post-run trace or telemetry-artifact copies are produced. Benchmark launches disable OpenClaw trajectory capture and the plugin standalone trace writer. LLM messages are retained without trace truncation. Supplemental records follow `contracts/trace-event.schema.json`: join `execution_telemetry` to tool spans by `execution_id`; its `artifact` contains the full eBPF result. Aborted PMU profiles are retained in `runtime_finalization` events. Missing completion hooks are explicitly recorded as `incomplete_span`; unmatched proxy captures are retained as `llm_proxy_unmatched`. Do not count these incomplete records as successful measurements. A successful drain requires trace persistence; abrupt process or machine failure can still lose unacknowledged in-memory events. KB files and OpenClaw session state remain operational data, not additional task trace exports.

For SWE timeouts/cancellations and interrupted Terminal agents, `traces/<task-digest>/runtime-finalization.json` records executions finalized after confirmed agent and sandbox shutdown. Measurements without confirmed tool exit remain incomplete and are withheld from learning; observations from previously completed tools remain available. Finalization does not turn a failed task into a successful task, even when `kb_flush_complete` is true.

```bash
.venv/bin/python tools/inspect_trace.py /path/to/run/traces/<task>/<file>.jsonl --all --details
python3 scripts/clawtune.py kb status --path /path/to/run/kb
```

An exit code alone does not validate a run when no tools were called, search setup failed, or collection was incomplete. Shared-service diagnostic copies must not be counted again as offline observations.

## 5. Fixed-trace offline evaluation

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

Choose `MB` or `MiB` according to the original RSS units; explicit byte-valued labels retain their units. Tasks need `instance_id` or `task_id`; repository tasks also need project identity. Adjacent `dataset-task.json` files from online runs can supply identity.

The split methodology is in the [technical report](technical-report.md#6-learning-initialization-and-evaluation). `--train-fraction` defaults to 0.8 and must lie strictly between 0 and 1. Each group assigns `max(1, floor(fraction*N))` tasks to training and the rest to testing. Assignments are reused by task roster, seed, and fraction. The registry defaults to `.runtime/offline/splits/`; use `--split-cache-dir` to relocate it.

| Output | Purpose |
| --- | --- |
| `split.json` | Task assignments and input hashes |
| `seed/` | Statistics constructed only from training data |
| `predictions.jsonl` | Test predictions and eligible labels |
| `report.json`, `report.md` | Availability, errors, baselines, and exclusions |

Mixed input is trained and evaluated separately per benchmark, with an aggregate report. Check train/test counts and `test_updates: 0` before interpreting metrics. All-singleton groups can leave no test set.
