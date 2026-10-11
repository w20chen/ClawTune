# Schema-driven command parsing: offline evaluation

The new parser is implemented, but **prediction accuracy did not improve on this split**. With matching and learning algorithms held fixed, Lattice and Edge gained 11 available duration predictions and lost bucket accuracy on their shared prediction set. Tool and Trie are unchanged controls. This result does not evaluate live LLM rule generation.

## Data and protocol

- Input: the user-specified `swe-rebench-original-flat-644-20260904` directory; exact source path is in [provenance.json](provenance.json).
- All 644 v5 trace files were registered, representing 631 unique task IDs; 13 tasks each have two traces. The metadata-only `MANIFEST.jsonl` is not a trace. No trace file was excluded by the inventory.
- Existing offline functional path, seed 42, training fraction 0.8, RSS unit `MiB`: 515 training tasks and 116 held-out tasks. Both traces for a task stay in the same split. This is task holdout, not a held-out-repository experiment.
- Phase1 baseline: commit `b513e1d`. Main: the source commit containing this report. Both runs use identical task assignments and identical SHA-256 values for all 644 source files. Split SHA-256: `3e709aa527044502706c1faf73968a30f22fabe7dd831d84247822a686ef7fcb`.
- Each run fits its own KBs from the training traces. No release seed was rebuilt or modified. Test-time updates are zero; bundled rules are fixed and LLM generation is disabled. The archived [rule manifest](rule-manifest.json) identifies the imported Fig revision and exact rule bytes.

## Duration results

Duration buckets have boundaries 100, 500, 2,000 and 10,000 ms. There are 3,185 labeled tool calls. Accuracy is evaluated only where a backend produced a prediction; the models have different scopes, so their raw accuracy numbers are not a fair cross-model ranking.

| Model | Available, phase1 → main | Accuracy on each run's available set | MAE on each run's available set |
| --- | ---: | ---: | ---: |
| Tool | 3,185 → 3,185 | 88.16% → 88.16% | 5.195 → 5.195 s |
| Trie | 1,216 → 1,216 | 71.30% → 71.30% | 13.686 → 13.686 s |
| Lattice | 1,205 → 1,216 | 69.46% → 67.27% | 13.244 → 14.519 s |
| Edge | 1,205 → 1,216 | 71.45% → 69.90% | 12.960 → 13.997 s |

Lattice/Edge coverage increases from 37.83% to 38.18% of labeled tool calls. The following comparison removes the coverage difference: **1,205 identical observations from 55 tasks**, with both versions providing predictions.

| Model | Paired bucket accuracy | Paired MAE | Accuracy change, 95% task-bootstrap interval |
| --- | ---: | ---: | ---: |
| Lattice | 69.46% → 67.47% | 13.244 → 13.501 s | −3.71 to −0.61 percentage points |
| Edge | 71.45% → 70.12% | 12.960 → 12.974 s | −2.58 to −0.08 percentage points |

Bootstrap: 2,000 task resamples, seed 42; all paired calls of each sampled task travel together. MAE-change intervals include zero: Lattice −0.677 to +1.478 s; Edge −1.210 to +1.374 s. These intervals describe this task split, not independent repetitions of the full study.

## Resource and parsing limits

There are 122 eligible CPU-peak labels in 14 test tasks. Paired Lattice MAE changes from 3.141 to 3.259 cores; its task-bootstrap change interval is −0.262 to +0.640 cores. Trie remains 3.591 cores. Tool/Edge provide no scored CPU-peak prediction here.

The offline loader produced no eligible held-out tool-call labels for total memory, extra memory, sampled peak RSS, CPU time, average CPU or PMU targets. Their accuracy cannot be concluded from this run. Historical clause-level RSS training observations are not interchangeable with eligible held-out tool-call labels.

Of 7,039 pre-execution test clause parses, 3,873 are complete, 3,095 partial and 71 invalid; two exceed the matching-feature budget. These are parser coverage diagnostics. The trace corpus does not contain independently annotated argument roles, so these counts are **not semantic parsing accuracy**. Manual parser fixtures separately test ownership, modes, aliases, boundaries and partial handling. Real provider generation success, cost and latency remain unmeasured.

## Reproduction and evidence

Run each checkout with its own `PYTHONPATH` and a fresh output directory:

```sh
PYTHONPATH=services/sidecar/src:. python -m benchmarks.cli offline \
  --dataset <the-specified-trace-directory> --benchmark swe-rebench \
  --rss-unit MiB --seed 42 --train-fraction .8 \
  --split-cache-dir <shared-split-directory> --output <new-run-directory>
```

Compressed raw predictions, reports and file-hashed splits are committed in [phase1/](phase1/) and [main/](main/). Recompute the paired comparison directly from these archives:

```sh
python scripts/command_rules/compare_offline.py \
  research/command_parser_evaluation/phase1 research/command_parser_evaluation/main \
  /tmp/command-parser-comparison.json
```

[comparison.json](comparison.json) contains paired metrics, confidence intervals and hashes of the uncompressed artifacts. [provenance.json](provenance.json) records unchanged release-seed hashes. No raw source traces are copied into this repository.

A separate fixed-feature probe checks the algorithm boundary: running `scripts/command_rules/fixed_feature_probe.py` under phase1 and main `PYTHONPATH`, with `PYTHONHASHSEED=0`, produces byte-identical Lattice node statistics, candidate/selection results, Edge learned snapshots and weighted predictions. The result and output digest are in [fixed-features.json](fixed-features.json). This verifies unchanged behavior on the probe's fixed inputs; it does not establish universal equivalence for every input.

Project validation commands and results are in [validation.json](validation.json). Remaining real-runtime acceptance commands are tracked in [CURRENT_PLAN.md](../../docs/CURRENT_PLAN.md).
