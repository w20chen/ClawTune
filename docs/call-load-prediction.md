# Tool-call load prediction

The public source of truth is [`call-load.schema.json`](../contracts/call-load.schema.json),
referenced by `tool-decision.schema.json`. The sidecar returns a versioned
`prediction.call_prediction` describing **one complete tool invocation**, with
optional `prediction.diagnostics.backends` containing comparable Runtime, Trie
and Lattice call-level candidates. Each candidate always declares all five targets.

## Targets and statistics

| Target | Unit | Definition |
| --- | --- | --- |
| `duration_ms` | ms | Elapsed tool-hook execution interval |
| `cpu_time_seconds` | core-seconds | CPU consumed by the owned workload |
| `cpu_avg_cores` | cores | Per-observation owned CPU time / tool interval |
| `cpu_peak_cores` | cores | Owned CPU peak using the collector's fixed 500 ms window |
| `memory_peak_rss_bytes` | bytes | Sampled peak RSS of distinct address spaces in the owned lineage |

The direct duration label is the existing completion event's `duration_ms`,
not a new reconstructed `tool_body_ns` label. It retains that event's hook
boundary/overhead convention; callers must not compare it to a body-only
measurement without converting the boundary explicitly.

Every available target contains `avg` (arithmetic mean), `p50` (median),
`p90` (nearest-rank empirical quantile), and `buckets` (edges and probabilities).
`avg` for `cpu_avg_cores` is the mean of per-execution averages, **not** the
ratio of the separate CPU-time and elapsed-time means or quantiles.

Buckets are `[0,e0), [e0,e1), ..., [last,infinity)`. An exact boundary belongs
to the bucket on its right. Probabilities have `len(edges)+1` entries and sum
to one. Zero CPU/RSS is a valid sample. Invalid samples do not become zeros.

Unavailable targets keep their unit, definition and configured edges, but all
statistics and bucket probabilities are null, with an explicit reason.
Python semantic validation additionally checks increasing edges, probability
normalization, full target coverage, target-unit/definition consistency and
`p50 <= p90` (cross-value constraints not expressible in ordinary JSON Schema).

`calibration: unvalidated` is deliberate. Neither evidence count nor maximum
bucket mass is a calibrated confidence. `evidence_counts` lists the historical
sample count for each component; `sample_count` is the number of samples used
to summarize the prediction and can include generated composition draws.

## Bucket configuration

Set comma-separated values in the root `.env` and restart the sidecar. No KB
retraining or snapshot rewrite is necessary when changing boundaries. Defaults:

| Environment variable | Default boundaries |
| --- | --- |
| `CLAWTUNE_TOOL_RESOURCE_LATENCY_BUCKETS_MS` | `100,500,2000,10000` (existing time configuration) |
| `CLAWTUNE_TOOL_RESOURCE_CPU_TIME_BUCKETS_S` | `0.01,0.1,1,10,60` |
| `CLAWTUNE_TOOL_RESOURCE_CPU_AVG_BUCKETS_CORES` | `0.1,0.5,1,2,4,8` |
| `CLAWTUNE_TOOL_RESOURCE_CPU_PEAK_BUCKETS_CORES` | `0.5,1,2,4,8,16` |
| `CLAWTUNE_TOOL_RESOURCE_MEMORY_BUCKETS_BYTES` | `16777216,67108864,268435456,1073741824,4294967296` |

Memory defaults correspond to 16, 64, 256, 1024 and 4096 MiB. Resource defaults
are broad workload-size ranges, not hardware limits or learned thresholds.
All lists must be nonempty, finite, positive and strictly increasing.
Programmatic callers can override `SidecarConfig.tool_resource_load_buckets`
per target; omitted resource targets retain their defaults. Time edges continue
to be controlled solely by `tool_resource_latency_buckets_ms`, including the
existing KV-TTL policy's bucket-count constraints.

## Backends and composition

Runtime retrieves compatible call-level samples. Trie retrieves clause samples
using exact/prefix/bin matching. Lattice selects a context independently for
each of the five targets (the adapter currently uses shrinkage; its evidence
API also supports LOSO and max-cardinality). All pass samples to the same
call adapter and statistics implementation; raw evidence is not serialized.

The authoritative selector is per-target: compatible Runtime call evidence,
then the Trie baseline, then Lattice. Each backend's own candidate remains in
diagnostics, so this policy does not hide backend coverage or imply measured
superiority. An unrelated global node is not a compatible canonical fallback.
Existing repository-first/prefix matching remains a heuristic, not an
environment-invariant similarity model.

The initial composer explicitly supports only literal foreground simple
commands and unconditional serial command lists. It checks clause spans and
the text between them, not just the parser's pipeline flags:

- A single clause can supply all five targets as a **composed estimate**.
  Assumptions explicitly say that its foreground lineage covers the workload
  and shell/hook overhead is not modeled. It is not relabeled as direct call
  measurement.
- Serial duration draws 2048 deterministic independent marginal samples, sums
  durations within each draw, then computes all statistics. It never sums
  clause medians/p90s or creates a one-hot bucket from a point estimate.
  Independence and foreground completion are explicit, unvalidated assumptions.
- Multi-clause CPU totals/averages and resource peaks remain unavailable until
  joint execution ownership and time alignment can be established. Existing
  clause lineage samples cannot prove non-overlap or peak concurrency.
- Pipelines, conditionals, shell loops, substitution, backgrounding, builtins,
  redirects/expansions and unsupported syntax do not get heuristic composition.
  Compatible direct call history can still predict such commands. Quoted
  metacharacters may conservatively disable composition too.
- Loop-, pipeline- and substitution-associated historical clauses are excluded
  from the new standalone clause views, independently of legacy diagnostics.

For asynchronous work, `tool_hook_interval` ends at hook completion; it does
not promise a prediction of a detached workload's eventual lifetime. Full job
lifecycle prediction would require explicit execution-ownership support.

## Observation semantics and migration

Live monitor CPU averages are no longer stored as CPU peaks. CPU totals are
preserved and average cores are derived from paired observations. Trace replay
reads `resources.cpu_time_s` (and accepts `cpu_time_delta_s` as an alternative).
The Runtime snapshot now writes `runtime_tool_resource_kb_v2`; v1 snapshots
remain readable, but their potentially mislabeled CPU peak nodes are
quarantined. Valid duration and legacy memory-residual diagnostics survive.
The benchmark snapshot synchronizer accepts both versions. No trace dataset
or shipped seed is rewritten by this source migration.

Canonical Runtime peak CPU requires an explicitly eligible 500 ms measurement.
Canonical Runtime memory requires eligible `sampled_distinct_mm_rss`. Current
coarse monitor memory may be cgroup `memory.current` or a process RSS sum;
neither is silently converted to this target. Thus the current live Runtime
path can lack these two targets even when legacy diagnostic memory exists.
Native tools without compatible call resource measurements report unavailable.
Clause eBPF evidence can supply canonical resources for supported exec calls.

Completed ordinary failures can contribute their measured call duration;
recognized timeout/cancel/abort/interruption outcomes are retained as censored
observations and excluded from complete canonical load labels. This is not
survival analysis, and error classification is limited to available event
metadata. Shared execution scopes are not accepted as per-call CPU/RSS labels.

The old `tool_resource` payload is retained as **deprecated diagnostics** during
migration. Its clause arrays and old bucket/continuous shapes are not the new
statistics contract. Top-level duration fields are compatibility aliases of
the authoritative distribution, rounded to milliseconds; `resource_class` is
derived from that same p90, and global `confidence` is null. KV-TTL uses the
authoritative duration histogram and p90. New consumers must use
`call_prediction`; legacy/custom predictors may omit it during migration.

Admission v3 reads only canonical call targets: CPU peak p90, then CPU-average
p90, then a one-core **policy default**, never a fabricated prediction. It
does not enforce memory placement; placement remains advisory. CPU empirical
quantiles are not guarantees and admission still uses existing capacity caps.

The single KB writer builds a Lattice successor from immutable observation
records outside the prediction lock, then publishes the prepared generation
under that lock. Queries retain the last complete generation while building;
strict `observation.end < query.start` visibility remains in each backend.
Failures retain the staged successor for a subsequent retry. Partial-pending
offline replay can still require rebuilding a causal subset.

## Evaluation and remaining limits

`python tools/evaluate_call_load.py held_out_calls.jsonl` scores recorded,
held-out **tool-call** predictions without changing data or training a KB.
Each JSONL record contains `scope: tool_call`, `lifecycle: tool_hook_interval`,
`prediction` (a `CallLoadPrediction`) and `actual` entries of
`{valid, value, metric_definition}` keyed by target. Incompatible measurement
definitions are rejected. Invalid/partial labels should set `valid: false`.

The report includes prediction availability, mean/p50 absolute error, p90
coverage and pinball loss, and histogram Brier score, separately per target.
Callers must construct causal held-out splits and compare each backend using
the same calls and labels; the scorer cannot detect train/test leakage itself.

The test suite covers statistics, boundaries, config validation, causal
visibility, snapshot migration, independent target masks, conservative
composition, schema agreement and admission consumption. Native parser/eBPF
integration and measured call-level calibration need Linux and compatible
held-out data; this change does **not** claim improved held-out accuracy.
CPU quotas, input sizes, interpreter/environment distinctions, correlated
clause sampling, and multi-clause aligned resource modeling remain future
modeling work rather than implicit assumptions disguised as measurements.
