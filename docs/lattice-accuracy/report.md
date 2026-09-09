# Lattice held-out accuracy

Train tasks: 239; test tasks: 38; seed: 42.

Frozen within-repository task split. No threshold or model tuning on this test run.
All errors use predicted test clauses; paired baseline uses exactly those same clauses.
Within-2x excludes zero actual values. WAPE = sum absolute error / sum actual.
p90 coverage should be assessed together with pinball loss, not maximized blindly.

| Target | Algorithm | N predicted/eligible | MAE | Median AE | WAPE | Within 2x | MAE reduction vs baseline | p90 coverage |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| latency_ms (s) | shrinkage | 1907/1907 | 1.899 | 0.03105 | 107.5% | 49.2% | -9.5% | N/A |
| latency_ms (s) | loso | 1907/1907 | 1.743 | 0.03535 | 98.7% | 51.2% | -0.6% | N/A |
| latency_ms (s) | max_cardinality | 1907/1907 | 1.858 | 0.02925 | 105.2% | 52.8% | -7.2% | N/A |
| cpu_time_seconds (core-s) | shrinkage | 1869/1907 | 0.83 | 0.004 | 91.6% | 33.4% | 0.1% | 80.5% |
| cpu_time_seconds (core-s) | loso | 1869/1907 | 0.7982 | 0.004 | 88.1% | 33.9% | 3.9% | 90.2% |
| cpu_time_seconds (core-s) | max_cardinality | 1869/1907 | 0.7945 | 0.004 | 87.7% | 39.3% | 4.3% | 86.1% |
| cpu_avg_cores (cores) | shrinkage | 1869/1907 | 0.61 | 0.01118 | 71.5% | 64.1% | -13.6% | 81.8% |
| cpu_avg_cores (cores) | loso | 1869/1907 | 0.532 | 0.008158 | 62.4% | 62.5% | 0.9% | 87.2% |
| cpu_avg_cores (cores) | max_cardinality | 1869/1907 | 0.61 | 0.01118 | 71.5% | 64.1% | -13.6% | 81.8% |
| cpu_peak_cores (cores) | shrinkage | 267/267 | 0.4805 | 0.01434 | 35.6% | 71.0% | -21.3% | 76.4% |
| cpu_peak_cores (cores) | loso | 267/267 | 0.443 | 0.01452 | 32.8% | 74.3% | -11.8% | 75.7% |
| cpu_peak_cores (cores) | max_cardinality | 267/267 | 0.5517 | 0.01553 | 40.9% | 65.3% | -39.3% | 81.6% |
| memory_peak_rss_bytes (MiB) | shrinkage | 852/869 | 36.58 | 9.638 | 61.7% | 65.5% | 5.5% | 81.3% |
| memory_peak_rss_bytes (MiB) | loso | 852/869 | 37.54 | 10.17 | 63.3% | 64.6% | 3.0% | 87.3% |
| memory_peak_rss_bytes (MiB) | max_cardinality | 852/869 | 36.79 | 9.638 | 62.0% | 65.6% | 4.9% | 81.9% |

## Larger realized workloads (shrinkage)

Post-hoc diagnostic subsets defined by actual resource use, not ex-ante scheduling classes.
Their conditional p90 coverage is not expected to equal the overall 90% target.

| Actual workload at least | Predicted/eligible | MAE | Within 2x |
|---|---:|---:|---:|
| latency_ms >= 1.0 s | 433/433 | 7.006 s | 21.2% |
| cpu_time_seconds >= 1.0 core-s | 247/247 | 5.487 core-s | 21.1% |
| cpu_avg_cores >= 0.8 cores | 890/916 | 0.9378 cores | 74.2% |
| cpu_peak_cores >= 1.0 cores | 128/128 | 0.813 cores | 68.0% |
| memory_peak_rss_bytes >= 128.0 MiB | 96/96 | 209 MiB | 10.4% |

## Tail loss vs paired baseline

Pinball loss penalizes underestimation more strongly at p90; lower is better.
Positive reduction means the lattice improves over the baseline on the same clauses.

| Resource | Algorithm | p90 pinball loss | Baseline loss | Loss reduction |
|---|---|---:|---:|---:|
| cpu_time_seconds (core-s) | shrinkage | 0.5407 | 0.55 | 1.7% |
| cpu_time_seconds (core-s) | loso | 0.49 | 0.55 | 10.9% |
| cpu_time_seconds (core-s) | max_cardinality | 0.4573 | 0.55 | 16.8% |
| cpu_avg_cores (cores) | shrinkage | 0.2884 | 0.261 | -10.5% |
| cpu_avg_cores (cores) | loso | 0.2607 | 0.261 | 0.1% |
| cpu_avg_cores (cores) | max_cardinality | 0.2884 | 0.261 | -10.5% |
| cpu_peak_cores (cores) | shrinkage | 0.1822 | 0.1199 | -51.9% |
| cpu_peak_cores (cores) | loso | 0.1763 | 0.1199 | -47.0% |
| cpu_peak_cores (cores) | max_cardinality | 0.151 | 0.1199 | -25.9% |
| memory_peak_rss_bytes (MiB) | shrinkage | 17.68 | 17.56 | -0.7% |
| memory_peak_rss_bytes (MiB) | loso | 18.8 | 17.56 | -7.1% |
| memory_peak_rss_bytes (MiB) | max_cardinality | 17.85 | 17.56 | -1.7% |

Full physical-unit errors, task-macro MAE, tail pinball losses and larger-workload strata are in metrics.json.
Per-clause actual/predicted pairs are in predictions.jsonl; command contents are replaced by hashes.
RSS is sampled clause-lineage RSS, not whole-call/cgroup memory. CPU source quota is 8 cores.
Time currently exposes point predictions only; no time p90 is invented in this evaluation.
Singleton repos are train-only. Results measure within-repo generalization, not unseen-repo deployment.
