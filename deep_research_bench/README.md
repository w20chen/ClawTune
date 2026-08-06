# Running Deep Research Bench with ClawTune

The batch runner sends DeepResearchBench research questions through OpenClaw
while ClawTune records model calls, tool calls, process lifecycle, CPU, and
memory.  Unlike SWE-Rebench there is **no per-task Docker image**: the agent's
tools execute in one very basic Docker sandbox image (default
`python:3.11-slim`), and there is no `/testbed` repository to solve — the task
is a research question answered with web-style tools.

## Configure Once

`python3 scripts/clawtune.py setup` creates `deep_research_bench/config.yaml`
from `deep_research_bench/config.example.yaml`.  Export the provider key in the
shell that starts the run:

```bash
export LLM_API_KEY="<provider-api-key>"
```

Or put the key on one line in the Git-ignored file
`deep_research_bench/llm_api_key.txt`.  Edit the model section in
`deep_research_bench/config.yaml`:

```yaml
llm:
  api_key_file: "./deep_research_bench/llm_api_key.txt"
  upstream_base_url: "https://api.deepseek.com"
  model: "your-model-name"
  openclaw_model_ref: "vllm/your-model-name"
```

## Web Search (Tavily)

DeepResearchBench tasks are answered with OpenClaw's built-in `web_search`
tool, which runs on the host (not inside the sandbox).  DRB defaults web
search to **Tavily**.  Configure the key the same way as the model key:

```bash
export TAVILY_API_KEY="<tavily-api-key>"
```

The wrapper preserves `TAVILY_API_KEY` (and `TAVILY_API_KEY_FILE`) through
`sudo` by name.  Persistent alternatives: put the key on one line in the
Git-ignored `deep_research_bench/tavily_api_key.txt`, or add a
`TAVILY_API_KEY=...` line to the root `.env`.  Resolution order:
`TAVILY_API_KEY` env, `web_search.api_key` (`${TAVILY_API_KEY}`),
`web_search.api_key_file`, then the root `.env`.

The `web_search` section of `deep_research_bench/config.yaml` controls the
provider:

```yaml
web_search:
  enabled: true
  provider: "tavily"   # set to "auto" to let OpenClaw auto-detect
  api_key: "${TAVILY_API_KEY}"
  api_key_file: "./deep_research_bench/tavily_api_key.txt"
```

Each task pins `tools.web.search.provider` into its isolated OpenClaw config
(OpenClaw auto-detection would otherwise prefer Brave over Tavily) and injects
`TAVILY_API_KEY` into the `openclaw agent` process.  No key is required: web
search is best-effort.  Without a key the agent answers from model knowledge
and the trace still records LLM spans; use `--no-gate-required` if such a run
should pass without any tool span.

## Select Tasks

The runner uses this order (like SWE-Rebench):

1. `--dataset <file>` — an explicit DeepResearchBench JSON/JSONL file always
   wins.
2. `--tasks <file>` — a simple JSON task list.
3. The bundled `deep_research_bench/tasks.json` — three smoke-test tasks.

To build a larger task source from the HuggingFace dataset
(`muset-ai/DeepResearch-Bench-Dataset`,
`generated_reports/openai-deepresearch.jsonl`):

```bash
# 32-task source (needs huggingface_hub)
python3 -m deep_research_bench.discover --sample 32 --out deep_research_bench/tasks-32.json

# Run it
python3 scripts/clawtune.py drb \
  --dataset deep_research_bench/tasks-32.json \
  --sample 32 --parallelism 1
```

## Run

Always start with one task:

```bash
python3 scripts/clawtune.py drb --sample 1
```

`clawtune.py drb` runs the `deep_research_bench.runner` under `sudo` (for the
sidecar's privileged eBPF runtime), prepares the runtime bundle, and exports
traces.  Per-task output lands under
`deep_research_bench/.runtime/traces/<task-id>/`, including the JSONL trace,
`agent_prompt.txt`, `task_manifest.json`, `reference_answer.txt` (record-only),
and `result_summary.json`.  A batch report is written to
`deep_research_bench/.runtime/report.json`.

### Telemetry gate

Research tasks use read/edit/web tools measured with the sandbox-container /
per-PID scope, so the swe-rebench eBPF exec-clause gate does not apply.
The relaxed gate (`runtime.gate_required`, default `true`) fails a task only
when its trace has no LLM span or no resource-sampled tool span.  Set
`runtime.gate_required: false` (or `--no-gate-required`) for a best-effort run.

## Direct runner usage

```bash
python3 -m deep_research_bench.runner run \
  --dataset deep_research_bench/tasks-32.json --sample 5 --export
python3 -m deep_research_bench.runner prepare   # build the runtime bundle once
```

Requires a Linux host (same as the SWE-Rebench journey): Docker, Node.js/npm,
OpenClaw 2026.7.1+, and the ClawTune `.venv` sidecar from `setup`.
