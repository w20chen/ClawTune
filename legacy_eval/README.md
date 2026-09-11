# Legacy Evaluation

`legacy_eval` retains observation-level splitting for older traces and dynamic cache-TTL analysis. Calls from the same task can appear on both sides of its split; this protocol must not be conflated with current task-held-out evaluation. Use the [benchmark guide](../docs/benchmarks.md#5-fixed-trace-offline-evaluation) for new experiments.

From the repository root:

```bash
python -m legacy_eval --help
python -m legacy_eval --dataset /data/legacy-traces \
  --train-frac 0.8 --seed 42 \
  --out .runtime/legacy/report.json --markdown .runtime/legacy/report.md
python scripts/evaluate_legacy_ttl_cost.py --help
python scripts/tune_legacy_shrinkage_kappa.py --help
```

Tune parameters within training data. Generated reports, per-observation records, and exported state belong in output directories, not in the source repository.
