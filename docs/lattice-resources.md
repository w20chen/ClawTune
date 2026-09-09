# Lattice CPU and memory predictions

The existing clause time lattice now keeps one raw observation log and builds
independent resource views alongside its unchanged time predictors. It outputs
all three algorithms (shrinkage, LOSO, max-cardinality) before an `exec` tool
runs. This change does not remove the existing RuntimeKB or latency-bucket KB.

## Metrics and outputs

| Target | Measurement | Public unit |
| --- | --- | --- |
| `cpu_time_seconds` | Owned lineage cumulative CPU nanoseconds / 1e9 | core-seconds |
| `cpu_avg_cores` | Each observation's CPU seconds / clause wall seconds | cores |
| `cpu_peak_cores` | Collector's maximum fixed 500 ms CPU window | cores |
| `memory_peak_rss_bytes` | Sampled, aligned distinct-mm RSS peak over the owned lineage | bytes |

These are **clause** measurements, not whole-call predictions. RSS is not
cgroup-accounted memory or an OOM-safe reservation. Compound commands expose
their executable clauses separately; there is no resource composition.
No average memory is inferred from endpoints or peak memory. Native shared
sandbox tools do not get these clause predictions.

The response adds `prediction.tool_resource.lattice_resource_predictions`.
Each clause has metric/scope metadata and twelve results: four targets times
three algorithms. The plugin prints resource p50/p90 alongside lattice time
estimates, and existing prediction traces retain the new payload.

Each resource target selects its own lattice context using only its eligible
observations. The selected samples provide p50 (ordinary median) and p90
(nearest-rank empirical quantile). A two-sample p50 is their arithmetic midpoint;
p90 is their maximum. The existing time point estimates are unchanged.
Memory risk calculations use MiB for numerical conditioning and convert output
back to bytes. CPU risk uses core-seconds or cores. Zero resource measurements
are valid; missing/invalid metrics do not become zero or invalidate other targets.

Unlike time's historical global fallback, resource queries with no matching
context return `no_matching_resource_context`. Empty targets report
`no_lattice_resource_evidence`; parsing and candidate-budget failures are explicit.
The inherited shrinkage exact-feature shortcut remains in place; a single sample
is not a calibrated estimate. Evidence count and selected features are exposed.

## Query-time thresholds

The Python `predict_resource_clauses` API optionally accepts a mapping such as
`thresholds={"memory_peak_rss_bytes": 536870912}`. It returns `probability_ge`,
the fraction of samples in the metric-selected node that meet the inclusive
threshold. No heavy definition, score, or boolean is saved in the KB. This is a
single-metric empirical query, not a joint-event classifier or a calibrated
confidence score. The sidecar's default output requests quantiles only.

## Online learning and compatibility

Completed eBPF observations feed the same writer and raw log as time. Prepared
node generations include resources. An observation becomes query-visible only
when its end timestamp is strictly before the query timestamp; equal-time and
overlapping observations remain pending. Time and resource queries run in the
same sidecar KB-lock transaction. Resource prediction failure does not remove
an otherwise valid time prediction.

The snapshot schema is now `clause_lattice_kb_v2`; the existing filename
`clause-lattice-time-kb.json` is retained for deployment discovery. The reader
accepts v1 snapshots, and the benchmark snapshot hand-off accepts both versions.
V2 permits resource-only observations with missing/zero wall duration, while
average CPU needs positive wall duration. Node statistics are rebuilt from raw
records; query thresholds never alter persisted observations.

Preparation still follows the existing full-rebuild lifecycle. It is performed
by the KB writer, but currently under the shared lock; large rebuilds can delay
concurrent queries. This change does not claim constant-time online updates or
an end-to-end scheduling latency guarantee.

## Cold start and held-out evaluation

Source: the user-supplied `D:/swe277-full-5be74da-20260726` dataset, read-only.
Tasks are grouped by repository; within each repository a deterministic shuffle
using seed 42 selects floor(0.8 * task count) training tasks, with at least one
train and one test task when there are multiple tasks. All attempts and clauses
of a task stay together. Singleton repositories are train-only and listed in
the manifest. This is a static task split, not the older observation-level split
or a chronological replay. Missing historical clause timestamps cannot establish
online ordering.

Result: 239 train tasks and 38 test tasks among 277 tasks. The 177 singleton
repositories explain why the overall train fraction exceeds 80%.

The shipped seed contains 11,253 eligible clause observations:

- CPU time and average cores: 11,253 each.
- Memory RSS peak: 5,053.
- CPU 500 ms peak: 1,368.

The exporter checks artifact health, call/clause eligibility, exec/exit boundary
coverage, exit signals, memory availability, and CPU peak window metadata. CPU
total remains usable when the *peak* target is unavailable for a short clause.
Rejected artifacts and source hashes are recorded. Source CPU quota is 8 cores;
predictions describe observed execution under source conditions, not unrestricted
CPU demand on arbitrary hardware. Environment-aware generalization remains future
work. No external trace files are modified.

Rebuild the seed and evaluate without feeding test observations back:

```powershell
python scripts/export_resource_lattice.py --dataset D:/swe277-full-5be74da-20260726 --seed 42 --train-fraction 0.8
python scripts/evaluate_resource_lattice.py --dataset D:/swe277-full-5be74da-20260726
```

The seed manifest records the exact task split and source/snapshot hashes.
`resource-lattice-evaluation.json` records held-out metrics and query timing.
For the initial held-out run, shrinkage prediction coverage was about 98% for
CPU time/average and memory, and 100% for eligible CPU peak observations.
Memory p90 covered approximately 81% of eligible predicted test observations;
CPU p90 coverage ranged approximately 76%-82%. **An empirical training p90 is
not a demonstrated 90% held-out bound.** Calibration is needed before treating
these predictions as hard resource reservations. The evaluation reports missing
predictions separately from quantile coverage, and all three algorithms.

## Validation

Tests cover unchanged latency numerics, independent target contexts, valid zero
values, missing wall time, invalid metrics, units and quantiles, threshold queries,
causal boundaries, prepared states, snapshot upgrade/deduplication, scope limits,
before-call payloads, failure isolation, schema validation, and task-level split
reproducibility/read-only input handling. Live Linux eBPF verification is distinct
from offline replay and cannot run on this Windows host; see CURRENT_PLAN.md.
