# Deep Research Bench with ClawTune

Deep Research Bench is one adapter behind the common online benchmark workflow.
It sends research questions through OpenClaw and records the same model/tool
trace protocol as other adapters. It has no per-task repository image or
`/testbed`; tools use the configured basic sandbox image.

Complete the root [installation guide](../docs/getting-started.md) first.

## Configure

The public runner reads `configs/benchmark.yaml`:

```bash
export LLM_API_KEY="<provider-api-key>"
export TAVILY_API_KEY="<tavily-api-key>"
```

Model settings live under `llm`; research-only settings live under `sandbox`
and `web_search`. Key files `configs/llm_api_key.txt` and
`configs/tavily_api_key.txt` are ignored by Git.

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

The files under `deep_research_bench/config*.yaml` configure the retained
internal/legacy runner. They remain readable through explicit `--config`, but
the common file above is the supported default.

## Task source

A task needs a non-empty `id`/`task_id`/`instance_id` and one of
`problem_statement`, `prompt`, or `question`. Optional `topic` or `domain`
selects the KB/report group. `reference_answer` or `article` is recorded but
not used as a live answer.

The source may be JSON, JSONL, or an object containing a `tasks`, `instances`,
or `data` array. With no explicit source, the runner checks the read-only
agent-test-bench dataset and then the bundled three-task smoke source.

To create a source from the upstream Hugging Face dataset:

```bash
python3 -m deep_research_bench.discover \
  --sample 32 --out deep_research_bench/tasks-32.json
```

## Run

Validate selection without Docker, OpenClaw, or an LLM:

```bash
python3 scripts/clawtune.py benchmark \
  --benchmark deep-research-bench \
  --dataset deep_research_bench/tasks-32.json --sample 2 --dry-run
```

Then start with one live task:

```bash
python3 scripts/clawtune.py benchmark \
  --benchmark deep-research-bench --sample 1
```

`python3 scripts/clawtune.py drb ...` is a compatibility alias.
`--parallelism N` bounds in-flight tasks (`1` is serial). Tasks do not wait for
one another; the sidecar coalesces their asynchronous KB updates through one
writer and the run performs one durability barrier after all tasks finish.

Outputs live under
`.runtime/benchmarks/deep-research-bench/<run>/` and use the same `run.json`,
`report.json`, `kb/`, `sidecar/`, and per-task trace layout as every adapter.
`official_score` remains `null`.

Research tasks do not require exec-clause telemetry. They still need at least
one resource-sampled tool span to produce learning observations; a task with no
tool span is reported as an error by the common runner.

For all adapter fields, state ownership, and evaluation boundaries, see the
[peer benchmark reference](../docs/MULTI_BENCHMARK_IMPLEMENTATION.md).
