# Benchmarks and Offline Evaluation

Online runs execute tasks through OpenClaw and collect predictions and measurements. Offline evaluation reads existing traces without model calls. The adapters support resource-prediction research; **they do not run official answer graders**. `official_score` is `null`, and a completed task is not necessarily solved correctly.

## 1. Configure a first run

Complete machine preparation in the [installation guide](getting-started.md). Setup creates `configs/benchmark.yaml`; copy the [configuration template](../configs/benchmark.example.yaml) to create another configuration.

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

Supply a key, validate selection, and run one task:

```bash
export LLM_API_KEY="<provider-api-key>"
python3 scripts/clawtune.py benchmark --list
python3 scripts/clawtune.py benchmark --sample 1 --dry-run
python3 scripts/clawtune.py benchmark --sample 1
```

Alternatively, place the raw key in the Git-ignored `configs/llm_api_key.txt`. Do not commit keys in YAML. Resolution order is: nonempty YAML value or environment expansion, exported `LLM_API_KEY`, configured key file, then root `.env`. `LLM_API_KEY_FILE` overrides the file path.

Defaults use external task lists when available; SWE and research otherwise use bundled smoke inputs. Use explicit dataset paths for experiments. Dry-run checks task structure, selection, and initialization data, not image availability, credentials, or live collection.

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

### SWE-Rebench

Supply an ID, `problem_statement`, and `docker_image`. The image must contain the prepared repository at `/testbed`. Provide `repo`, or use an `owner__repo-issue` ID from which it can be inferred:

```json
[{"instance_id":"org__repo-1","repo":"org/repo","problem_statement":"Fix the issue","docker_image":"registry/task:tag"}]
```

Prepare an input list and run:

```bash
.venv/bin/python -m pip install datasets
.venv/bin/python -m swe_rebench.discover --sample 2 --out .runtime/swe-tasks.json
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset .runtime/swe-tasks.json --sample 1 --dry-run
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset .runtime/swe-tasks.json --sample 1
```

Discovery first attempts the external task source, then Hugging Face. Use the dataset-provided image, not a generic Python image. Execution requires an exclusive cgroup and valid command-level eBPF measurements.

### SWE-bench Verified

Uses the same repository fields with a separate dataset identity. Export upstream records and run:

```bash
.venv/bin/python -m pip install datasets
mkdir -p .runtime/datasets
.venv/bin/python -c "from datasets import load_dataset; load_dataset('princeton-nlp/SWE-bench_Verified', split='test').to_json('.runtime/datasets/verified.jsonl')"
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified \
  --dataset .runtime/datasets/verified.jsonl --sample 1
```

Without an explicit image, the adapter derives the official x86_64 image name from the task ID. ARM hosts require the emulation setup described in the installation guide.

### Deep Research Bench

Supply an ID and nonempty `problem_statement`, `prompt`, or `question`. Optional `topic` or `domain` controls grouping. Reference answers are saved with output, not given to the executing model.

```bash
.venv/bin/python -m pip install huggingface_hub
openclaw plugins install @openclaw/tavily-plugin
.venv/bin/python -m deep_research_bench.discover --source hf \
  --sample 2 --out .runtime/research-tasks.json
export TAVILY_API_KEY="<tavily-key>"
python3 scripts/clawtune.py benchmark --benchmark deep-research-bench \
  --dataset .runtime/research-tasks.json --sample 1
```

The template uses `python:3.11-slim` and the required `/workspace` mount. Search uses the [Tavily plugin](https://docs.openclaw.ai/tools/tavily). Credentials can also be stored in `configs/tavily_api_key.txt`. If OpenClaw has a `plugins.allow` list, add `tavily` while preserving other trusted plugins. Inspect the task's `web-search-config.log` to confirm provider setup.

`web_search.enabled: false` disables search. Research tasks require tool events but do not guarantee repository-style command, CPU, or RSS attribution.

### BFCL

Prepare the [Gorilla BFCL package](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard) and its dependencies. If using an existing read-only checkout, copy it before installation so build metadata stays outside the source dataset:

```bash
mkdir -p .runtime/dependencies
cp -R /data/gorilla/berkeley-function-call-leaderboard .runtime/dependencies/bfcl
export BFCL_REPO_PATH="$PWD/.runtime/dependencies/bfcl"
.venv/bin/python -m pip install "$BFCL_REPO_PATH"
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --category multi_turn_base --sample 2 --dry-run
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --category multi_turn_long_context --sample 2
```

Replace `/data/gorilla` with the actual checkout. Use a fresh copy when changing revisions. Native category loading needs these dependencies even in dry-run.

Function state persists across turns of one task; tasks are independent. Supported cases include base, long-context, and applicable search categories. Memory/prerequisite chains, dependent tasks, dynamic function additions, and AST-only inputs are unsupported. Search categories use `SERPAPI_API_KEY`, not Tavily credentials.

With `--dataset`, provide processed entries containing `id`, turn-structured `question`, `function` documentation, and `involved_classes`, optionally `initial_config`. Raw category files missing function documentation are not equivalent to loader output.

### Terminal Bench

Supports [Terminal Bench v1](https://github.com/laude-institute/terminal-bench) `task.yaml` with Compose or a Dockerfile, not Harbor/v2 `task.toml`. Obtain a v1 task checkout and use its actual task directory:

```bash
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --dataset /data/terminal-bench/original-tasks --sample 2 --dry-run
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --dataset /data/terminal-bench/original-tasks --sample 1
```

A single task directory or `task.yaml` is also accepted. In JSON lists, `task_path` resolves relative to the list file, for example `[{"task_path":"tasks/my-task"}]`.

Tasks are copied into the run before container creation. Compose must provide exactly one `client` container, and host mounts/build contexts must remain within allowed run paths. Each tool invocation uses a fresh shell: use explicit `cd` and files for persistent state. Persistent interactive TTYs are unsupported. This adapter provides hook duration, not attributed command-level CPU/RSS.

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
| `traces/<task-digest>/` | Per-task traces, model output, and logs |
| `kb/` | Learned statistics for the run |
| `sidecar/` | Shared monitoring-service logs |
| `workspaces/` | Task workspaces |

Check `status`, each result's `error` and `exit_code`, and `kb_flush_complete: true`, which indicates completed persistence:

```bash
.venv/bin/python tools/inspect_trace.py /path/to/run/traces/<task>/<file>.jsonl --all --details
python3 scripts/clawtune.py kb status --path /path/to/run/kb
```

An exit code alone does not validate a run when no tools were called, search setup failed, or collection was incomplete. Shared-service diagnostic copies must not be counted again as offline observations.

## 5. Fixed-trace offline evaluation

Install the [development dependencies](getting-started.md#5-development-checks). No provider key, Docker execution, or running collector is needed. Inputs are v5/v6 JSONL traces with `trace_metadata`, **not online task lists**.

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

Keep generated outputs locally or in external storage. For reproduction, archive the code revision, dependency versions, input hashes, split, configuration, and full command outside the code repository. The retained [legacy evaluator](../legacy_eval/README.md) uses an older observation-level protocol; use this section for new evaluations.
