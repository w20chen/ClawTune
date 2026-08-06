# legacy_eval

Independent offline evaluation of ClawTune's tool-resource prediction
algorithms over external **legacy-format** trace datasets.

The problem this solves: we do not have the time/resources to re-run a legacy
trace dataset through the ClawTune harness, but we still want to measure how
well ClawTune's prediction algorithms would perform on those traces.  This
package adapts the external dataset format, trains the prediction KBs on a
random 80% of tasks, and evaluates them on the held-out 20%.

It is deliberately **independent**: it never modifies the vendored algorithm
code under `services/scheduler/src/tool_resource` / `tool_time`.  It only
parses the legacy format, constructs the algorithm input objects
(`ClauseObservation` / `CompletedCall`), and drives the public KB APIs.

## Algorithms evaluated

| track | algorithm | source | granularity |
| --- | --- | --- | --- |
| `clause_latency_bucket` | empirical latency-bucket classifier | `ClauseResourceKB` | clause |
| `lattice_shrinkage` | Bayesian-shrinkage point prediction | `LatticeTimeKB` | clause |
| `lattice_loso` | leave-one-signature-out point prediction | `LatticeTimeKB` | clause |
| `lattice_max_cardinality` | max-cardinality point prediction | `LatticeTimeKB` | clause |
| `continuous_latency_p90` | conditional p90 latency | `RuntimeToolResourceKB` | tool call |
| `continuous_cpu_p90` | conditional p90 CPU | `RuntimeToolResourceKB` | tool call |

The production sidecar additionally exposes `quantile_mlp`, which is disabled
in production and not evaluated here.  `peak_memory_mb` is not evaluated: the
legacy format has no ambient-memory anchor, so the honest result is
"unavailable".

## Evaluation protocol

* **Split**: random 80% train / 20% test **by task** (one task = one
  workspace/repo history, so no repo's commands leak between splits).
  Deterministic for a given `--seed` (default 42).
* **Static train/test**: the KBs are built **only** from the training split;
  the test split is replayed predict-only and is never fed back into the KB.
  This measures cross-task (cold-start) generalization.

## Legacy format supported

```text
<dataset>/
  <org>__<repo>-<pr>/
    attempt_1/
      clause_telemetry.json   # Stage-2 clause artifact (ClawTune-valid)
      trace.jsonl             # action-level trace (llm_call + tool_exec)
      ...                     # unused (resources.json etc.)
```

`clause_telemetry.json` is structurally identical to ClawTune's own Stage-2
clause telemetry and passes the native `tool_resource.sdk._load_valid_artifact`
validation unchanged.  `trace.jsonl` supplies the call-level view (tool name,
command, duration, success, timestamps) used by the continuous predictor.

Known format limitations (reported honestly in the output):

* clause rows carry no `ts_start`/`ts_end` (causality is not available);
* `resources.json` has no per-call memory samples (monitoring disabled), so
  continuous memory predictions are unavailable.

## Usage

```bash
# Full run (defaults: dataset=D:\swe100-full-5be74da-20260726, 80/20, seed=42)
python -m legacy_eval

# Explicit options
python -m legacy_eval \
  --dataset D:\swe100-full-5be74da-20260726 \
  --train-frac 0.8 --seed 42 \
  --bucket-edges 100,500,2000,10000 \
  --out legacy_eval/.runtime/report.json --markdown legacy_eval/.runtime/report.md \
  --print-summary

# Smoke test (few tasks)
python -m legacy_eval --max-train-tasks 4 --max-test-tasks 2 --print-summary
```

Outputs a full JSON report (`report.json`, all per-call records) and a
Markdown summary (`report.md`).  Results are written under
`legacy_eval/.runtime/` (gitignored) by default.

## Cold-start KB export

The KBs trained on the training split can be serialized into the project's
cold-start snapshot format (`traces/tool-resource/`) so the runtime sidecar
loads them as its seed on the next benchmark run:

```bash
# Export to the project cold-start seed (default training = 80 tasks, seed=42)
python -m legacy_eval --export-kb traces/tool-resource --skip-eval

# Stage elsewhere first, then review before replacing
python -m legacy_eval --export-kb legacy_eval/.runtime/coldstart --skip-eval
```

The export writes the three snapshots the project validates and loads
(`clause-resource-kb.json`, `clause-lattice-time-kb.json`,
`runtime-tool-resource-kb.json`; schemas `runtime_clause_resource_kb_v4`,
`clause_lattice_time_kb_v1`, `runtime_tool_resource_kb_v1`).

Design decisions (confirmed with the user):

* **Source**: the 80 training tasks of the seed-42 split (the same KB the
  evaluation measures).
* **Public layer only**: the per-repo layer is left empty because the 80
  legacy task repos are not the workspaces the project will run.
* **Memory prior**: legacy traces carry no ambient-memory anchor, so the
  export preserves the existing seed's `peak_memory_mb` global prior (merged
  from `traces/tool-resource/runtime-tool-resource-kb.json`).

Library use:

```python
from legacy_eval.export import export_cold_start_kb_dataset

export = export_cold_start_kb_dataset(r"D:\swe100-full-5be74da-20260726",
                                      out_dir="traces/tool-resource")
print(export.to_json_obj())
```

## Library use

```python
from legacy_eval.engine import EvalConfig, evaluate_dataset
from legacy_eval.loader import load_all
from legacy_eval.report import render_markdown

result = evaluate_dataset(r"D:\swe100-full-5be74da-20260726",
                          config=EvalConfig(seed=7))
print(render_markdown(result))
```

## Tests

```bash
python -m pytest tests/test_legacy_eval.py tests/test_legacy_eval_export.py \
  -q --basetemp .pytest-tmp-root
```
