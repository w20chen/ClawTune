# Cold-start knowledge transfer study

Start with [the research report](final_results/research_report.md) and [validation evidence](final_results/validation.md). The output directory also contains data/implementation/leakage audits, E1–E3 CSVs, repository bootstrap comparisons, learning curves, figures, failure cases and individual fold predictions.

Reproduce from the repository root using the existing sidecar environment plus NumPy and Matplotlib:

```sh
python research/cold_start_transfer/reproduce.py \
  --flat /path/to/swe-rebench-original-flat-644-20260904 \
  --output /path/to/new-study-output
```

The output directory must not exist. Keep the real `.runtime` trace downloads referenced by `final_data/source_manifest.json` in this checkout; no benchmark dataset is downloaded or executed by this command. Source traces are read-only. The runner records code/data hashes, configuration and logs, then executes all supported repository-disjoint folds and generates the report.

`n` counts valid **clause observations for the evaluated target**, not tool calls. Calibration and test tasks are disjoint. Curves use the same test identities across supported `n`; `balanced_learning_curve.csv` additionally keeps the same repositories across `n`. The event sequence is a reproducible simulated serial order, not reconstructed online concurrency.

Read availability with error. `e3_kb_ablation.csv` compares methods with Lattice on common predictions. RSS, environment memory sources, hardware conditions and collection runs remain separate. An absent confidence interval means fewer than five independent repositories. A missing candidate prediction can mean no source-only calibration validation was possible.

Protocol checks:

```sh
python -m pytest research/cold_start_transfer/test_protocol.py -q
```

Synthetic values in protocol tests are never experiment observations.

To compare a fresh run against the delivered results, including every fold prediction and CSV:

```sh
python research/cold_start_transfer/verify.py \
  --data research/cold_start_transfer/final_data \
  --results research/cold_start_transfer/final_results \
  --rerun /path/to/new-study-output
```

This also recomputes qualified clause memory labels from the original sampled timelines. The delivered `memory_scope_quarantine.json` retains labels rejected because the measured cgroup did not contain the execution; an original `eligible` flag alone is insufficient.
