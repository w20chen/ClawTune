# ClawTune Legacy Evaluation — Final Report

> Final results for the SWE-bench-style legacy dataset `swe277`.
> Run: `legacy_eval/.runtime/2026-08-06T160709.445174_0000/` (report.json / report.md)

## 1. Setup & Configuration

| Item | Value |
| --- | --- |
| Dataset | `<dataset-root>` (277 tasks / 213 repos) |
| Protocol | static train/test, **latt-style observation-level split** (tool-call granularity) |
| Split rule | group tool calls by `<org>__<repo>`; repos with ≥10 calls contribute `int(n×0.2)` (≥1) calls to test (seeded shuffle); smaller repos train-only |
| Seed / split | `seed=42`, `train_frac=0.8` (deterministic, reproducible) |
| Preprocessing | per-clause telemetry (clause_telemetry.json); **trivial pipe consumers excluded** (`tail/head/wc/cat/tee/cut/tr`); repo key = `<org>__<repo>` prefix |
| Latency buckets | `[600, 2000, 10000, 60000]` ms → 5 buckets (b0<0.6s, b1 0.6–2s, b2 2–10s, b3 10–60s, b4 >60s) |
| Algorithms | `clause_latency_bucket` (ClauseResourceKB), `shrinkage`/`loso`/`max_cardinality` (LatticeTimeKB), `continuous_latency_p90`/`cpu_p90`/`memory_p90` (RuntimeToolResourceKB) |
| Hyperparameters | fixed defaults (no tuning): shrinkage `κ=5`, `α=0.03`, `δ=0.15`; loso `m_min=2`; estimator=median; `max_optional_features=6`; exact-match shortcut enabled for shrinkage |

### Data counts

| metric | value |
| --- | --- |
| train / test tool calls | 10 797 / 2 555 |
| train / test clause observations | 7 547 / 1 703 (eligible 7 435) |
| train / test tasks (distinct) | 277 / 277 |

## 2. Time prediction — four algorithms (1 703 test clauses, coverage 100%)

### 2.1 Overall

| Algorithm | Top-1 bucket acc. | F1 (macro) | F1 (weighted) | Precision | Recall | Brier |
| --- | --- | --- | --- | --- | --- | --- |
| **clause_latency_bucket** | **80.0%** | 0.563 | **0.801** | 0.614 | 0.539 | **0.321** |
| **shrinkage** (bucketed) | **80.3%** | 0.572 | 0.792 | 0.693 | 0.515 | n/a |
| loso (bucketed) | 77.7% | 0.457 | 0.779 | 0.667 | 0.442 | n/a |
| **max_cardinality** (bucketed) | **80.8%** | 0.583 | **0.809** | 0.685 | 0.551 | n/a |

### 2.2 Per-bucket accuracy / F1 (bucket, support)

| Bucket | support | clause_latency_bucket | shrinkage | loso | max_cardinality |
| --- | --- | --- | --- | --- | --- |
| b0 <0.6s | 1241 | 91.1% / F1 .917 | **93.2%** / .898 | 88.6% / .909 | 90.2% / .912 |
| b1 0.6–2s | 223 | 60.5% / .530 | 52.0% / .504 | **74.4%** / .496 | 72.2% / .557 |
| b2 2–10s | 182 | 42.3% / .467 | 41.2% / .524 | 28.6% / .416 | 42.3% / .535 |
| b3 10–60s | 42 | 35.7% / .380 | 31.0% / .433 | 9.5% / .167 | 31.0% / .433 |
| b4 >60s | 15 | 40.0% / .522 | 40.0% / .500 | 20.0% / .300 | 40.0% / .480 |

### 2.3 Point metrics (lattice, ms)

| Metric | shrinkage | loso | max_cardinality |
| --- | --- | --- | --- |
| MAE | 1844.0 | 1905.3 | 1877.8 |
| Median absolute error | 22.4 | 28.3 | 21.5 |
| Relative error | 7.1 | 3.4 | 7.7 |
| Mean predicted / actual | 1199 / 2362 | 864 / 2362 | 1278 / 2362 |
| Predicted / actual p90 | 1591 / 3280 | 1591 / 3280 | 1597 / 3280 |

### 2.4 Confusion matrices (rows=actual, cols=predicted)

`clause_latency_bucket`:

| | b0 | b1 | b2 | b3 | b4 |
| --- | --- | --- | --- | --- | --- |
| b0 (1241) | 1130 | 68 | 37 | 5 | 1 |
| b1 (223) | 57 | 135 | 24 | 6 | 1 |
| b2 (182) | 24 | 71 | 77 | 10 | 0 |
| b3 (42) | 8 | 12 | 7 | 15 | 0 |
| b4 (15) | 5 | 0 | 3 | 1 | 6 |

`shrinkage`: b0 1157/64/17/2/1 · b1 97/116/7/2/1 · b2 58/48/75/0/1 · b3 15/9/5/13/0 · b4 8/0/0/1/6
`loso`: b0 1099/136/6/0/0 · b1 51/166/5/1/0 · b2 23/105/52/0/2 · b3 3/30/5/4/0 · b4 1/10/0/1/3
`max_cardinality`: b0 1119/102/17/2/1 · b1 51/161/7/2/2 · b2 36/68/77/0/1 · b3 6/18/5/13/0 · b4 2/6/0/1/6

## 3. Continuous resource prediction (2 555 test tool calls)

| Target | coverage | pinball(q=.9) | mean pred / actual | pred q / actual q |
| --- | --- | --- | --- | --- |
| latency (ms) | 100% | 1275.9 | 3004.6 / 2050.2 | 4608.8 / 1720.7 |
| CPU (cores) | 12.3% | 0.111 | 2.213 / 1.602 | 3.796 / 2.621 |
| memory (MB) | 0.0% | n/a | n/a | n/a |

Notes: CPU coverage is partial because only clauses ≥1 s carry peak-CPU samples. Memory is **not evaluable** on this dataset (`resources.json` has `monitoring_disabled: true`, zero samples, no ambient anchor); the memory path is implemented as absolute-value prediction but coverage is 0.

## 4. Key findings (brief analysis)

1. **All four time algorithms reach ~80% top-1 bucket accuracy** with the `600/2000/10000/60000` buckets: 80.0–80.8% (loso 77.7%). Short-command bucket (b0) carries 73% of the test and is the best-predicted segment (88–93%).
2. **shrinkage is strongest on short commands**: b0 accuracy 93.2% — the highest of all four methods. It is also robust on medium-evidence (3–10 samples) clauses where loso degrades (−3 pp). Head-to-head with max_cardinality is near-identical (41 vs 50 exclusive wins); shrinkage is more conservative on fast/failed calls, max_cardinality better on long pytest runs.
3. **Trivial-pipe exclusion was the decisive data fix**: pipe-inherited latencies (e.g. a `tail` measured at 556 s) polluted 33.8% of clauses; excluding `tail/head/wc/cat/tee/cut/tr` lifted bucket accuracy from 73.0%→76.8% (previous edges) and lattice relative error from ~48–75%→3–7%.
4. **Continuous latency p90** over-predicts the q0.9 tail (pred 4609 vs actual 1721 ms) — the long-runner tail is under-sampled in the empirical quantile.
5. **Bucket boundary choice affects the headline number** (76.8% at 100/1000/10000/60000 → 80.0% at 600/2000/10000/60000): the 600 ms edge folds the well-predicted short commands into one large b0. This is a metric-definition effect, not a model change; the point predictions are identical across edge sets.

## 5. Reproduction

Requirements: Python 3.12 + numpy; run from the repo root; `services/scheduler/src` on `PYTHONPATH`.

```bash
# Run the full evaluation (final configuration)
$env:PYTHONPATH = "services/scheduler/src"          # PowerShell
python -m legacy_eval --dataset "<dataset-root>" \
    --bucket-edges 600,2000,10000,60000

# Unit tests
python -m pytest tests/test_legacy_eval.py tests/test_legacy_eval_export.py -q

# Analysis helpers
python scripts/analyze_lattice_worst.py <report.json> loso
python scripts/compare_lattice_segments.py <report.json>
python scripts/lattice_head_to_head.py <report.json>
python scripts/sweep_buckets.py <report.json>
```

Determinism: split uses `random.Random(f"{seed}:{repo}")` (string-seeded, stable across runs / `PYTHONHASHSEED`); same seed ⇒ identical train/test split and results. Full per-sample records and the split keys are stored in the run's `report.json`.

### Code changes for this evaluation (all under `legacy_eval/`, plus two scripts)

- `legacy_eval/split.py` — `repo_prefix`, `split_tasks_by_repo` (task-level, for export), `split_observations_by_repo` (latt-style tool-call split).
- `legacy_eval/engine.py` — observation-level partition (`_partition_observations`), repo-layer commit, `continuous_memory_p90` track, per-bucket classification for lattice (`*_bucket` summaries), per-class accuracy.
- `legacy_eval/loader.py` — trivial-pipe-tool exclusion in `parse_clause_artifact`.
- `legacy_eval/metrics.py` — per-class (per-bucket) accuracy in `summarize_bucket`.
- `legacy_eval/report.py` — per-bucket F1/accuracy tables, observation-split header.
- `scripts/analyze_lattice_worst.py`, `scripts/compare_lattice_segments.py`, `scripts/lattice_head_to_head.py`, `scripts/sweep_buckets.py` — analysis helpers.
- `services/scheduler/src/tool_time/lattice_kb.py` — explicit `exact_match_shortcut=True` for shrinkage (behavior already auto-enabled; pinned explicitly).
