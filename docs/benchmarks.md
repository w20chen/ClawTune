# Online benchmark guide

The unified entry point is `python3 scripts/clawtune.py benchmark`. It runs
OpenClaw with one run-owned sidecar and writable KB. All five adapters are
simulations for tracing and prediction/learning; none runs an official grader.
Complete [installation](getting-started.md) before live execution.

## Data and paths

`--dataset` (alias `--tasks`) accepts a JSON array, JSONL, or a JSON object
containing a `tasks`, `instances` or `data` array. Terminal also accepts a native
task directory, its `task.yaml`, or a directory of immediate child tasks.
Task IDs must be unique. Paths in commands are relative to the repository root;
Terminal paths **inside a JSON task list** are relative to that list's directory.
Absolute paths and `~` paths are accepted by the task loader.

With no explicit source, the external root is `$AGENT_TEST_BENCH_ROOT`, or the
sibling `../agent-test-bench`. These candidates are checked in order:

| Adapter | Default source |
| --- | --- |
| SWE-Rebench | `<external>/data/swe-rebench/tasks.json`, then `swe_rebench/tasks.json` |
| Deep Research Bench | `<external>/data/deep-research-bench/tasks.json`, then `deep_research_bench/tasks.json` |
| SWE-bench Verified | `<external>/data/swe-bench-verified/tasks.json`, then `<external>/data/swebench_verified/tasks.json` |
| Terminal Bench | `<external>/data/terminal-bench/tasks.json`, then `<external>/data/terminal-bench/tasks/` |
| BFCL | Native category loader from the separately installed BFCL package |

These are lookup conventions, not evidence of downloaded data. The bundled
SWE and research lists are small smoke sources, not full datasets.
Other layouts require `--dataset`; the runner does not guess paths from trace
folders or fetch a whole benchmark implicitly. External inputs remain read-only.

```bash
python3 scripts/clawtune.py benchmark --list
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset /data/swe.jsonl --sample 2 --dry-run
```

Dry-run checks input structure, selection and seed hashes. It does not validate
Docker images, backend dependencies, credentials or eBPF. BFCL native loading
requires its dependencies even in dry-run and can initialize import-time cache
folders under `.runtime/bfcl`; a processed JSON source avoids native imports.

## SWE-Rebench

Required fields: `instance_id` (or `task_id`/`id`), `problem_statement`, and a
dataset-provided `docker_image` (also `image`/`image_name`). Supply `repo`, or use
a standard `owner__repo-issue` ID from which it can be inferred. `base_commit`
is optional. Images must contain the prepared repository at `/testbed`.

```json
{"instance_id":"org__repo-1","repo":"org/repo","problem_statement":"Fix the issue","docker_image":"registry/task:tag"}
```

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-rebench \
  --dataset /data/swe.jsonl --sample 1
```

The executor exports `/testbed` into the task workspace, prepares OpenClaw's
sandbox, and requires managed execution cgroups and valid eBPF clause artifacts.
The upstream dataset is [nebius/SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench).
The retained `python -m swe_rebench.discover --help` utility can prepare input;
use its output explicitly with the public command.

## SWE-bench Verified

Uses the same repository fields and executor, with a separate dataset identity
and namespace. If no image is supplied, the adapter derives
`docker.io/swebench/sweb.eval.x86_64.<instance-id>:latest`, replacing `__` with
`_1776_` and lowercasing it. An explicit image overrides this fallback.

Export [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)
records to JSON/JSONL, then run:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified \
  --dataset /data/verified.jsonl --sample 1
```

On arm64 the default x86_64 images need Docker/QEMU support; the sidecar stays
native. A valid image name is not proof the registry is reachable.

## Deep Research Bench

Required: an ID and nonempty `problem_statement`, `prompt` or `question`.
Optional `topic`/`domain` chooses the group; `reference_answer`/`article` is saved
beside the trace and is not supplied as the agent's answer. This adapter uses
OpenClaw web tools and a basic sandbox, not a repository image.

```bash
# Optional upstream download: install into the setup-created interpreter.
.venv/bin/python -m pip install huggingface_hub
.venv/bin/python -m deep_research_bench.discover --source hf \
  --sample 32 --out .runtime/research-tasks.json
export TAVILY_API_KEY="<tavily-key>"
python3 scripts/clawtune.py benchmark --benchmark deep-research-bench \
  --dataset .runtime/research-tasks.json --sample 1
```

The discovery source is
[muset-ai/DeepResearch-Bench-Dataset](https://huggingface.co/datasets/muset-ai/DeepResearch-Bench-Dataset),
`generated_reports/openai-deepresearch.jsonl`. Local JSONL can be used directly.
The `drb` wrapper is a compatibility alias for this benchmark.

Configure `sandbox.image` and `web_search` in `configs/benchmark.yaml`.
`sandbox.workdir` is fixed at `/workspace`. Tavily uses `TAVILY_API_KEY` or
`configs/tavily_api_key.txt`; its provider plugin must be available to OpenClaw.
The runner attempts to link a discovered provider installation into each task
home, and may fall back to auto-detection if unavailable. Inspect
`web-search-config.log` to verify the actual outcome. `web_search.enabled: false`
explicitly disables search; it does not disable every other network tool.

Research requires tool spans but not repository-style exec-clause evidence.
Missing resource attribution remains unavailable; a span alone does not prove
an eligible CPU/RSS observation or a KB update.

## BFCL

Install the [Gorilla BFCL package](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)
and its dependencies into the runner's `.venv`. Keep the external checkout
outside ClawTune outputs:

```bash
# Build from a copy so pip cannot write build metadata into read-only data.
mkdir -p .runtime/dependencies
cp -R /data/gorilla/berkeley-function-call-leaderboard .runtime/dependencies/bfcl
export BFCL_REPO_PATH="$PWD/.runtime/dependencies/bfcl"
.venv/bin/python -m pip install "$BFCL_REPO_PATH"
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --category multi_turn_base --sample 2 --dry-run
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --category multi_turn_long_context --sample 2
```

`BFCL_REPO_PATH` accepts either the Gorilla root or its
`berkeley-function-call-leaderboard` package directory. If omitted, the package
must already be importable. BFCL import-time output/cache paths are redirected
to `.runtime/bfcl`, including Python bytecode caches, not the external checkout.
Use a fresh dependency-copy destination when changing revisions.
Record the BFCL revision used for
an experiment; upstream interfaces and data are separate dependencies.

A `--dataset` file must contain processed entries: `id`, `question` as a list
of turns of system/user text messages, `function` schemas, `involved_classes`,
and optional `initial_config`. Raw BFCL files that omit predefined function
docs are not equivalent to loader output. Native instances and the OpenClaw
session persist across turns; each independent task starts fresh state.

Web-search categories use BFCL's **`SERPAPI_API_KEY`**, not the research adapter's
Tavily key. Export it in the launch shell; the wrapper preserves it through sudo.

Unsupported inputs are rejected: AST-only rows, memory/prerequisite chains,
nonempty `depends_on`, and `missed_function` dynamic tool additions. The runner
has no dependency scheduler or per-turn tool registry changes. Accepting these
as ordinary independent tasks would change their intended semantics.

## Terminal Bench

Supports the [v1 task format](https://github.com/laude-institute/terminal-bench):
`task.yaml` plus Compose or a Dockerfile. Harbor/Terminal-Bench 2 `task.toml`
inputs are not supported. Obtain a v1 task checkout and pass the actual task
parent (`tasks/` or `original-tasks/`, depending on the checkout):

```bash
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --dataset /data/terminal-bench/original-tasks --sample 2 --dry-run
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --dataset /data/terminal-bench/original-tasks --sample 1
```

Alternatively: `[{"task_path":"tasks/my-task"}]` in a JSON list. Identity uses
`instance_id`, `task_id` or `id` (in that order), otherwise the directory name.
The task is
copied into the run before Docker Compose is resolved. Dockerfile-only tasks
get a single `client` service using their own build context. Compose must expose
exactly one `client` container; standard `T_BENCH_*` variables are provided,
including the task name prefix. Docker Compose v2 is required.

Host bind mounts must resolve within the run and build contexts within the
copied task. Tasks relying on external host paths are rejected. Tool calls use
`sh -lc` in the client container; each call is a fresh shell, so use explicit
`cd` and files for persistent state. There is no interactive TTY or official
grader. This path records hook latency, not attributed clause CPU/RSS.

## Shared controls and results

`--sample N` selects the first N tasks, not a random sample. Selection order is
repository filter, ordered `--instance-ids a,b`, then `--skip`, then `--sample`.
Insufficient or duplicate selections are errors. `--repo` applies to repository
adapters. Set `--parallelism N`, or `batch.parallelism` in the configuration;
`1` is serial. Task and agent timeout overrides accept seconds; `0` disables a
limit. Cleanup has separate bounded waits. Terminal additionally honors
`task.yaml`'s positive `max_agent_timeout_sec` (v1 default 360) across the whole
agent conversation after setup. The earlier of that budget and the configured
task deadline wins; shell calls also have a 300-second cap. Disabling the CLI
timeout does not disable the task's native agent budget.

Each invocation starts a KB from `--seed` (default `seeds/bootstrap-v1`). Workers have
separate homes/workspaces and share one sidecar predictor. Learning follows
actual completion order; exact concurrent interleaving is not reproducible.
After workers finish, every real runtime is drained before one KB flush.
Ctrl+C cancels workers and preserves recorded results; interrupted learning
cannot be safely replayed.

Default output: `.runtime/benchmarks/<benchmark>/<run>/`. `--output` must name a
new directory outside input datasets/seeds. The run contains `run.json`,
`report.json`, `kb/`, `sidecar/`, `traces/<task-digest>/` and `workspaces/`.
Bridged adapters additionally own their backend environments/logs.

Check `status`, each result's `error`/`exit_code`, `kb_flush_complete` and
`kb_final_generation`. Per-task generation changes can include peer updates;
they do not attribute learning to that task. Inspect one trace at a time:

```bash
python tools/inspect_trace.py /path/to/run/traces/<task>/<file>.jsonl --all --details
python3 scripts/clawtune.py kb status --path /path/to/run/kb
python3 scripts/clawtune.py benchmark --resume /path/to/run --config /path/to/original.yaml
```

Resume requires the same config and seed (pass the original `--seed` if custom),
uses saved tasks/parallelism, and does not retry recorded failures. Active tasks
or an incomplete final barrier make a run non-resumable. Start a new run instead.
[Configuration](configuration.md) describes key resolution and overrides;
[current validation](CURRENT_PLAN.md) distinguishes unit checks from live acceptance.
