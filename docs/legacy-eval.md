# Reproduction Guide for Offline Evaluation on the Legacy Dataset

This document explains how to reproduce the four types of ClawTune results on the
legacy trace dataset:

1. Continuous-time point predictions;
2. Time-bucket Accuracy/F1 for the four algorithms;
3. The residency cost `C_R` and invalidation metric `C_M` of dynamic KV-TTL;
4. The offline sweep of the shrinkage hyperparameter `kappa`.

For the base module documentation and all CLI options, see
[`legacy_eval/README.md`](../legacy_eval/README.md).

## 1. Environment and data

The published SWE277 experiment uses:

- Python 3.12;
- Data directory:
  `<dataset-root>`;
- 277 `<org>__<repo>-<pr>` task directories;
- Each valid attempt contains at least `clause_telemetry.json` and `trace.jsonl`;
- `train_frac=0.8`;
- `seed=42`.

Offline evaluation does not require Docker, cgroup, or eBPF. Commands must be run
from the repository root. `legacy_eval/_bootstrap.py` automatically loads
`services/scheduler/src`.

It is recommended to first define a PowerShell variable:

```powershell
$dataset = "<dataset-root>"
```

## 2. Current split protocol

The currently reported protocol is named `static_train_test_obs_per_repo` and uses
a per-repo, observation-level static split:

1. Aggregate tool calls by `<org>__<repo>`;
2. For repos with at least 10 calls, put
   `max(1, int(n * (1-train_frac)))` calls into the test side;
3. The shuffle is determined by seed; repos with few calls are used entirely for
   training;
4. Clauses follow their corresponding calls into train or test via
   `tool_call_id`;
5. Build `ClauseResourceKB`, `LatticeTimeKB`, and `RuntimeToolResourceKB` using
   only the train side;
6. The test side only predicts and never writes back to the KB.

It is not a task-level mutually exclusive split. The same task directory may
contain both training and test calls, so the distinct number of train/test tasks
reported can both be 277.

Results are reproducible when the data, code, dependencies, `train_frac`, and seed
are fixed.

## 3. The four time algorithms

| Name | Output mode |
| --- | --- |
| `clause_latency_bucket` | Outputs discrete time buckets directly |
| `shrinkage` | Outputs continuous time, then mapped to the same time buckets |
| `loso` | Outputs continuous time, then mapped to the same time buckets |
| `max_cardinality` | Outputs continuous time, then mapped to the same time buckets |

The point predictions of the last three algorithms do not change when the bucket
boundaries change; the boundaries only change which bucket they are mapped to.
Therefore, a change in Accuracy under different boundaries is not equivalent to
the predicted time itself becoming more accurate.

## 4. Reproducing the time-bucket results

Using boundaries `600/2000/10000/60000 ms`:

```powershell
$out = "legacy_eval\.runtime\bucket-swe277-b600-2000-10000-60000-seed42"

python -m legacy_eval `
  --dataset $dataset `
  --train-frac 0.8 `
  --seed 42 `
  --bucket-edges "600,2000,10000,60000" `
  --out "$out\report.json" `
  --markdown "$out\report.md"
```

The corresponding five right-open buckets:

| Bucket | Time range | SWE277 test samples |
| --- | --- | ---: |
| b0 | `[0,0.6)s` | 1241 |
| b1 | `[0.6,2)s` | 223 |
| b2 | `[2,10)s` | 182 |
| b3 | `[10,60)s` | 42 |
| b4 | `[60,+∞)s` | 15 |

Expected overall results:

| Algorithm | Coverage | Top-1 Accuracy | Macro F1 | Weighted F1 |
| --- | ---: | ---: | ---: | ---: |
| `clause_latency_bucket` | 100% | 80.04% | 0.5631 | 0.8014 |
| `shrinkage` | 100% | 80.27% | 0.5721 | 0.7918 |
| `loso` | 100% | 77.75% | 0.4574 | 0.7785 |
| `max_cardinality` | 100% | 80.80% | 0.5834 | 0.8093 |

Outputs:

- `report.json`: configuration, split, per-sample records, and all summary
  metrics;
- `report.md`: point-prediction metrics, per-bucket Accuracy/F1, and confusion
  matrix.

## 5. Reproducing the dynamic TTL cost

Policy configuration:

```text
bucket boundaries = [0.6, 2, 10, 60] s
TTL by bucket     = [0.6, 2, 0, 0, 0] s
```

Run:

```powershell
python scripts/evaluate_legacy_ttl_cost.py `
  --dataset $dataset `
  --out-dir "legacy_eval\.runtime\ttl-cost-swe277-b600-2000-10000-60000-seed42" `
  --train-frac 0.8 `
  --seed 42 `
  --bucket-edges-ms "600,2000,10000,60000" `
  --ttl-by-bucket-s "0.6,2,0,0,0"
```

The runtime bucket and cost are defined as:

```text
k(t) = max(k0, bucket reached at elapsed time t)
D    = first time the active bucket has TTL=0 or t >= active TTL
C_R  = min(T,D)
C_M  = 1[T>D]
```

When a boundary is reached, the new bucket is entered first, and then the new
bucket's TTL is evaluated. Result files:

- `ttl_cost_summary.json`;
- `ttl_cost_summary.csv`;
- `ttl_cost_report.md`;
- `ttl_cost_records.jsonl`.

Expected results:

| Algorithm | `C_R` total/s | `C_R` mean/s | `C_M` count | `C_M` ratio |
| --- | ---: | ---: | ---: | ---: |
| `clause_latency_bucket` | 547.859285 | 0.321702 | 313 | 18.38% |
| `shrinkage` | 621.509170 | 0.364950 | 269 | 15.80% |
| `loso` | 695.250559 | 0.408250 | 251 | 14.74% |
| `max_cardinality` | 616.707150 | 0.362130 | 270 | 15.85% |

In this experiment, all four algorithms cover 1703 samples, so each algorithm's
support set and the four-algorithm common support set are identical. If coverage
differs in the future, compare total cost using the common support set.

## 6. Sweeping shrinkage kappa

The production default is `kappa=5.0`. The script below only overrides the
parameter in the current offline process and rebuilds all shrinkage variances for
each candidate; it does not modify the production default:

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

Expected sweep results:

| kappa | Bucket Accuracy | Macro F1 | MAE/ms | `C_M` |
| ---: | ---: | ---: | ---: | ---: |
| 0.5 | 80.3288% | 0.572808 | 1843.660 | 269 |
| 1 | 80.3288% | 0.572808 | 1843.687 | 269 |
| 2 | 80.3288% | 0.572808 | 1843.687 | 269 |
| 3 | 80.2701% | 0.572090 | 1844.019 | 269 |
| 5 | 80.2701% | 0.572090 | 1844.019 | 269 |

This sweep directly inspects the test side, so it should only be treated as an
exploratory result. For formal hyperparameter tuning, build a validation split
inside the train side or check multiple seeds, and evaluate the final test set
only once after selecting the parameter.

## 7. Validation and archiving results

Unit tests:

```powershell
python -m pytest tests/test_legacy_eval.py tests/test_legacy_eval_export.py -q `
  --basetemp .pytest-legacy-eval
python -m pytest services/scheduler/tests/test_kv_ttl.py -q `
  --basetemp .pytest-kv-ttl
```

Quick pipeline check:

```powershell
python -m legacy_eval --dataset $dataset `
  --max-train-tasks 10 --max-test-tasks 5 --print-summary
```

The two `--max-*-tasks` flags limit the number of distinct task directories
represented on each side of the observation-level split. All selected
observations from each retained task remain on that side. These flags are
intended for smoke tests.

`legacy_eval/.runtime/` is ignored by Git by default. When publishing results,
record at least:

- The full dataset path or snapshot identifier;
- Git revision;
- Python and dependency versions;
- `train_frac` and seed;
- Bucket boundaries;
- TTL policy;
- All tuned hyperparameters;
- The full command and `report.json`/per-sample records.

## 8. Other operations

Export the train-side cold-start KB:

```powershell
python -m legacy_eval --dataset $dataset `
  --export-kb "legacy_eval\.runtime\coldstart" --skip-eval
```

Analyze the generated `report.json`:

```powershell
python scripts/analyze_lattice_worst.py <report.json> loso
python scripts/compare_lattice_segments.py <report.json>
python scripts/lattice_head_to_head.py <report.json>
python scripts/sweep_buckets.py <report.json>
```

For a historical summary, see
[`docs/legacy_eval_final_report.md`](legacy_eval_final_report.md), but the
reproduction commands and current protocol follow this document and
`legacy_eval/README.md`.
