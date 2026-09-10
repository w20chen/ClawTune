# Lattice held-out accuracy

Train tasks: 239; test tasks: 38; seed: 42.

Frozen within-repository task split. No threshold or model tuning on this test run.
All errors use predicted test clauses; paired baseline uses exactly those same clauses.
Within-2x excludes zero actual values. WAPE = sum absolute error / sum actual.
p90 coverage should be assessed together with pinball loss, not maximized blindly.

| Target | Algorithm | N predicted/eligible | MAE | Median AE | WAPE | Within 2x | MAE reduction vs baseline | p90 coverage |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| latency_ms (s) | shrinkage | 1309/1309 | 1.1 | 0.006491 | 90.8% | 57.5% | 1.4% | N/A |
| latency_ms (s) | loso | 1309/1309 | 1.054 | 0.0084 | 87.0% | 61.5% | 5.5% | N/A |
| latency_ms (s) | max_cardinality | 1309/1309 | 1.041 | 0.008804 | 85.9% | 62.6% | 6.7% | N/A |
| cpu_time_seconds (core-s) | shrinkage | 1271/1309 | 1.219 | 0.004004 | 91.6% | 36.2% | 0.1% | 76.8% |
| cpu_time_seconds (core-s) | loso | 1271/1309 | 1.173 | 0.008 | 88.1% | 39.7% | 3.9% | 89.2% |
| cpu_time_seconds (core-s) | max_cardinality | 1271/1309 | 1.167 | 0.004 | 87.6% | 43.5% | 4.3% | 85.1% |
| cpu_avg_cores (cores) | shrinkage | 1271/1309 | 0.7157 | 0.05581 | 64.8% | 78.0% | -17.6% | 81.3% |
| cpu_avg_cores (cores) | loso | 1271/1309 | 0.633 | 0.05131 | 57.3% | 77.5% | -4.0% | 87.0% |
| cpu_avg_cores (cores) | max_cardinality | 1271/1309 | 0.7157 | 0.05581 | 64.8% | 78.0% | -17.6% | 81.3% |
| cpu_peak_cores (cores) | shrinkage | 224/224 | 0.5723 | 0.02749 | 35.6% | 76.8% | -23.7% | 75.9% |
| cpu_peak_cores (cores) | loso | 224/224 | 0.5276 | 0.02828 | 32.8% | 80.4% | -14.0% | 74.6% |
| cpu_peak_cores (cores) | max_cardinality | 224/224 | 0.6571 | 0.03056 | 40.9% | 70.5% | -42.0% | 82.1% |
| memory_peak_rss_bytes (MiB) | shrinkage | 761/778 | 40.92 | 12.05 | 61.7% | 64.7% | 5.5% | 81.2% |
| memory_peak_rss_bytes (MiB) | loso | 761/778 | 42 | 12.01 | 63.4% | 62.9% | 3.0% | 86.6% |
| memory_peak_rss_bytes (MiB) | max_cardinality | 761/778 | 41.16 | 11.85 | 62.1% | 64.8% | 4.9% | 81.9% |

## Larger realized workloads (shrinkage)

Post-hoc diagnostic subsets defined by actual resource use, not ex-ante scheduling classes.
Their conditional p90 coverage is not expected to equal the overall 90% target.

| Actual workload at least | Predicted/eligible | MAE | Within 2x |
|---|---:|---:|---:|
| latency_ms >= 1.0 s | 224/224 | 5.456 s | 22.8% |
| cpu_time_seconds >= 1.0 core-s | 247/247 | 5.487 core-s | 21.1% |
| cpu_avg_cores >= 0.8 cores | 844/870 | 0.777 cores | 78.1% |
| cpu_peak_cores >= 1.0 cores | 128/128 | 0.813 cores | 68.0% |
| memory_peak_rss_bytes >= 128.0 MiB | 96/96 | 209 MiB | 10.4% |

## Tail loss vs paired baseline

Pinball loss penalizes underestimation more strongly at p90; lower is better.
Positive reduction means the lattice improves over the baseline on the same clauses.

| Resource | Algorithm | p90 pinball loss | Baseline loss | Loss reduction |
|---|---|---:|---:|---:|
| cpu_time_seconds (core-s) | shrinkage | 0.7947 | 0.8085 | 1.7% |
| cpu_time_seconds (core-s) | loso | 0.7201 | 0.8085 | 10.9% |
| cpu_time_seconds (core-s) | max_cardinality | 0.672 | 0.8085 | 16.9% |
| cpu_avg_cores (cores) | shrinkage | 0.2755 | 0.2497 | -10.3% |
| cpu_avg_cores (cores) | loso | 0.2406 | 0.2497 | 3.7% |
| cpu_avg_cores (cores) | max_cardinality | 0.2755 | 0.2497 | -10.3% |
| cpu_peak_cores (cores) | shrinkage | 0.2169 | 0.1403 | -54.6% |
| cpu_peak_cores (cores) | loso | 0.2099 | 0.1403 | -49.6% |
| cpu_peak_cores (cores) | max_cardinality | 0.1797 | 0.1403 | -28.1% |
| memory_peak_rss_bytes (MiB) | shrinkage | 19.78 | 19.65 | -0.6% |
| memory_peak_rss_bytes (MiB) | loso | 21.04 | 19.65 | -7.1% |
| memory_peak_rss_bytes (MiB) | max_cardinality | 19.98 | 19.65 | -1.7% |

Full physical-unit errors, task-macro MAE, tail pinball losses and larger-workload strata are in metrics.json.
Per-clause actual/predicted pairs are in predictions.jsonl; command contents are replaced by hashes.
RSS is sampled clause-lineage RSS, not whole-call/cgroup memory. CPU source quota is 8 cores.
Time currently exposes point predictions only; no time p90 is invented in this evaluation.
Singleton repos are train-only. Results measure within-repo generalization, not unseen-repo deployment.
