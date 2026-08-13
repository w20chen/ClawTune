# legacy_eval

`legacy_eval` is the independent offline evaluator for ClawTune's prediction
algorithms on external legacy-format traces. It reads previously captured
telemetry, builds the same knowledge bases used by the scheduler, and replays a
static held-out split without modifying OpenClaw core or the prediction
algorithms.

For an end-to-end reproduction guide, including the published SWE277
commands, dynamic KV-TTL costs, and shrinkage-kappa sweeps, see
[`docs/legacy-eval.md`](../docs/legacy-eval.md).

## Supported legacy layout

```text
<dataset-root>/
  <org>__<repo>-<pr>/
    attempt_1/
      clause_telemetry.json
      trace.jsonl
      ...
```

`clause_telemetry.json` supplies executable-clause latency and resource labels.
`trace.jsonl` supplies the call-level tool name, command, duration, status,
timestamps, and tool-call ID. The large top-level simulation JSONL is not read.

Known limitations are preserved in the report:

- legacy clause rows do not contain real clause start/end timestamps;
- the corpus has no per-call ambient memory samples, so continuous memory
  coverage is zero;
- short clauses often have no eligible peak-CPU label, so CPU coverage is
  partial.

## Evaluation protocol

The current protocol is `static_train_test_obs_per_repo`:

1. Tool calls are grouped by `<org>__<repo>`.
2. Repositories with at least 10 calls contribute
   `max(1, int(n * (1 - train_frac)))` seeded, shuffled calls to test; smaller
   repositories remain training-only.
3. Clause rows follow their `tool_call_id`. Missing or unmatched IDs remain on
   the training side.
4. Knowledge bases are built from training observations only.
5. The test observations are replayed predict-only and never update the KB.

This is an **observation-level split**, not a disjoint task-level split. One
task directory can contain both training and test calls, so the report may show
the same number of distinct train and test tasks. The historical CLI flag names
`--max-train-tasks` and `--max-test-tasks` cap the number of distinct task
directories represented on each side; all selected observations from each
retained task stay on that side. These flags are intended for smoke tests.

The split is deterministic for a fixed dataset, `train_frac`, seed, code
revision, and dependency set. The published SWE277 runs use `train_frac=0.8`
and `seed=42`.

## Prediction tracks

| Report key | Algorithm | Granularity | Bucket result |
| --- | --- | --- | --- |
| `clause_latency_bucket` | empirical bucket classifier | executable clause | predicted directly |
| `shrinkage` | Bayesian-shrinkage lattice selector | executable clause | point prediction mapped through the configured edges |
| `loso` | leave-one-signature-out lattice selector | executable clause | point prediction mapped through the configured edges |
| `max_cardinality` | most-specific matching lattice node | executable clause | point prediction mapped through the configured edges |
| `continuous_latency_p90` | conditional p90 | tool call | not part of the four-algorithm bucket comparison |
| `continuous_cpu_p90` | conditional p90 | tool call | not part of the four-algorithm bucket comparison |
| `continuous_memory_p90` | conditional p90 | tool call | unavailable for this legacy corpus |

## Reproduce the SWE277 bucket report

Run from the repository root. `_bootstrap.py` adds
`services/sidecar/src` to `sys.path`; no manual `PYTHONPATH` is required.

PowerShell:

```powershell
$dataset = "<dataset-root>"
$out = "legacy_eval\.runtime\bucket-swe277-b600-2000-10000-60000-seed42"

python -m legacy_eval `
  --dataset $dataset `
  --train-frac 0.8 `
  --seed 42 `
  --bucket-edges "600,2000,10000,60000" `
  --out "$out\report.json" `
  --markdown "$out\report.md"
```

The five right-open buckets are `[0,0.6)`, `[0.6,2)`, `[2,10)`,
`[10,60)`, and `[60,+inf)` seconds. The output contains point metrics,
bucket accuracy/F1, per-bucket metrics, confusion matrices, coverage, and all
per-sample records.

Expected headline metrics for the specified SWE277 snapshot:

| Algorithm | Coverage | Bucket accuracy | Macro F1 | Weighted F1 |
| --- | ---: | ---: | ---: | ---: |
| `clause_latency_bucket` | 100% | 80.04% | 0.5631 | 0.8014 |
| `shrinkage` | 100% | 80.27% | 0.5721 | 0.7918 |
| `loso` | 100% | 77.75% | 0.4574 | 0.7785 |
| `max_cardinality` | 100% | 80.80% | 0.5834 | 0.8093 |

## Dynamic KV-TTL costs

The TTL evaluator uses the scheduler's pure dynamic policy implementation:

```text
k(t) = max(k0, bucket reached by elapsed time t)
C_R  = min(T, D)
C_M  = 1[T > D]
```

At an exact boundary the runtime advances to the new bucket before evaluating
its TTL. Reproduce the published policy with boundaries
`600,2000,10000,60000 ms` and TTLs `0.6,2,0,0,0 s`:

```powershell
python scripts/evaluate_legacy_ttl_cost.py `
  --dataset $dataset `
  --out-dir "legacy_eval\.runtime\ttl-cost-swe277-b600-2000-10000-60000-seed42" `
  --train-frac 0.8 `
  --seed 42 `
  --bucket-edges-ms "600,2000,10000,60000" `
  --ttl-by-bucket-s "0.6,2,0,0,0"
```

Outputs:

- `ttl_cost_summary.json`: machine-readable configuration and aggregates;
- `ttl_cost_summary.csv`: compact comparison table;
- `ttl_cost_report.md`: human-readable report;
- `ttl_cost_records.jsonl`: one cost row per algorithm and test clause.

The report provides both per-algorithm available support and the common support
of all four algorithms. Always compare totals on common support if coverage
differs.

## Shrinkage-kappa sweep

The production shrinkage strength is `kappa=5.0`. The offline sweep rebuilds
all lattice node variances for every candidate while leaving the production
constant unchanged:

```powershell
python scripts/tune_legacy_shrinkage_kappa.py `
  --dataset $dataset `
  --out-dir "legacy_eval\.runtime\shrinkage-kappa-small-swe277-b600-seed42" `
  --kappas "0.5,1,2,3,5" `
  --bucket-edges-ms "600,2000,10000,60000" `
  --ttl-by-bucket-s "0.6,2,0,0,0" `
  --train-frac 0.8 `
  --seed 42
```

This is exploratory tuning on the evaluation split. Select a candidate on an
inner validation split or multiple seeds before changing the production
default, then report the final test result only once.

## Other CLI operations

Smoke test:

```powershell
python -m legacy_eval --dataset $dataset `
  --max-train-tasks 10 --max-test-tasks 5 --print-summary
```

Restrict the corpus using a JSON list or `{ "tasks": [...] }` object:

```powershell
python -m legacy_eval --dataset $dataset --task-list tasks.json
```

Export the training side as the three cold-start KB snapshots:

```powershell
python -m legacy_eval --dataset $dataset `
  --export-kb "legacy_eval\.runtime\coldstart" --skip-eval
```

Review a staged export before copying it into `traces/tool-resource`. The
export writes `clause-resource-kb.json`, `clause-lattice-time-kb.json`, and
`runtime-tool-resource-kb.json`.

## Output and validation

`legacy_eval/.runtime/` is ignored by Git. Preserve the command, code revision,
dataset path/snapshot, seed, split fraction, bucket boundaries, TTL policy, and
any tuned hyperparameters alongside published numbers.

Run the evaluator tests from the repository root:

```powershell
python -m pytest tests/test_legacy_eval.py tests/test_legacy_eval_export.py -q `
  --basetemp .pytest-legacy-eval
python -m pytest services/sidecar/tests/test_kv_ttl.py -q `
  --basetemp .pytest-kv-ttl
```

Useful post-processing helpers for a generated `report.json`:

```powershell
python scripts/analyze_lattice_worst.py <report.json> loso
python scripts/compare_lattice_segments.py <report.json>
python scripts/lattice_head_to_head.py <report.json>
python scripts/sweep_buckets.py <report.json>
```
