# Tool-call load prediction

The public source of truth is [`call-load.schema.json`](../contracts/call-load.schema.json),
referenced by `tool-decision.schema.json`. The sidecar returns a versioned
`prediction.call_prediction` describing **one complete tool invocation**, with
optional `prediction.diagnostics.backends` containing comparable ToolKB, TrieKB
and LatticeKB call-level candidates. Each candidate always declares all five targets.

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

ToolKB retrieves compatible call-level samples. TrieKB retrieves clause samples
using exact/prefix/bin matching. LatticeKB selects a context independently for
each of the five targets (the adapter currently uses shrinkage; its evidence
API also supports LOSO and max-cardinality). All pass samples to the same
call adapter and statistics implementation; raw evidence is not serialized.

The authoritative selector is per-target: compatible ToolKB call evidence,
then the TrieKB baseline, then LatticeKB. Each backend's own candidate remains in
diagnostics, so this policy does not hide backend coverage or imply measured
superiority. An unrelated global node is not a compatible canonical fallback.
Existing repository-first/prefix matching remains a heuristic, not an
environment-invariant similarity model.

The composer supports literal foreground simple commands, unconditional serial
command lists, and simple pipelines. It checks clause spans and the text
between them as well as the parser's pipeline flags:

- A single clause can supply all five targets as a **composed estimate**.
  Assumptions explicitly say that its foreground lineage covers the workload
  and shell/hook overhead is not modeled. It is not relabeled as direct call
  measurement.
- Serial duration draws 2048 deterministic independent marginal samples, sums
  durations within each draw, then computes all statistics. It never sums
  clause medians/p90s or creates a one-hot bucket from a point estimate.
  Independence and foreground completion are explicit, unvalidated assumptions.
- Pipeline duration takes the maximum sampled duration within each concurrent
  pipeline group. Downstream dependency-only consumers (`tail`, `head`, `wc`,
  `grep`, `cat`, and the related configured set) are omitted before querying
  evidence. The same binaries remain eligible standalone and at pipeline
  position zero.
- Multi-clause CPU totals/averages and resource peaks remain unavailable until
  joint execution ownership and time alignment can be established. Existing
  clause lineage samples cannot prove non-overlap or peak concurrency.
- Conditionals, shell loops, substitution, backgrounding, builtins, general
  redirects/expansions and unsupported syntax do not get heuristic composition.
  The common stderr merge `2>&1` is accepted in a simple pipeline.
  Compatible direct call history can still predict such commands. Quoted
  metacharacters may conservatively disable composition too.
- Loop- and substitution-associated historical clauses are excluded from the
  standalone clause views. Pipeline producers remain eligible; configured
  downstream consumers are excluded using `in_pipe` and `pipeline_position`.

For asynchronous work, `tool_hook_interval` ends at hook completion; it does
not promise a prediction of a detached workload's eventual lifetime. Full job
lifecycle prediction would require explicit execution-ownership support.

## Observation semantics and migration

Live monitor CPU averages are no longer stored as CPU peaks. CPU totals are
preserved and average cores are derived from paired observations. Trace replay
reads `resources.cpu_time_s` (and accepts `cpu_time_delta_s` as an alternative).
The ToolKB snapshot now writes `runtime_tool_resource_kb_v2`; v1 snapshots
remain readable, but their potentially mislabeled CPU peak nodes are
quarantined. Valid duration and legacy memory-residual diagnostics survive.
The benchmark snapshot synchronizer accepts both versions. No trace dataset
or shipped seed is rewritten by this source migration.

Canonical ToolKB peak CPU requires an explicitly eligible 500 ms measurement.
Canonical ToolKB memory requires eligible `sampled_distinct_mm_rss`. Current
coarse monitor memory may be cgroup `memory.current` or a process RSS sum;
neither is silently converted to this target. Thus the current live ToolKB
path can lack these two targets even when legacy diagnostic memory exists.
Native tools without compatible call resource measurements report unavailable.
Clause eBPF evidence can supply canonical resources for supported exec calls.

Completed ordinary failures can contribute their measured call duration;
recognized timeout/cancel/abort/interruption outcomes are retained as censored
observations and excluded from complete canonical load labels. This is not
survival analysis, and error classification is limited to available event
metadata. Shared execution scopes are not accepted as per-call CPU/RSS labels.

The flat cold-start exporter follows the same censoring rule. It checks
structured lifecycle fields on the action, resource observation and timeline,
retains censored call records for accounting, and withholds their clause
observations as well as complete call labels. It does not infer truncation from
command text or tool output, or from an ordinary nonzero exit. The seed report
includes `censored_calls` and `withheld_censored_clause_observations` counts.

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

The single KB writer builds a LatticeKB successor from immutable observation
records outside the prediction lock, then publishes the prepared generation
under that lock. Queries retain the last complete generation while building;
strict `observation.end < query.start` visibility remains in each backend.
Failures retain the staged successor for a subsequent retry. Partial-pending
offline replay can still require rebuilding a causal subset.

## Evaluation and remaining limits

### Console output

The backend names below are ToolKB, TrieKB and LatticeKB. Current console
labels may still display `Runtime`, `Trie` and `Lattice`, respectively; these
are legacy labels for the same backends, not additional KBs.

With plugin `consoleMode=verbose` (the default, also selectable with
`CLAWTUNE_CONSOLE_MODE=verbose`), each successful decision prints the selected
five-target prediction, a PMU section, then separate ToolKB, TrieKB and LatticeKB
candidates. The PMU section always contains IPC, LLC read MPKI, and LLC read
miss rate. Each metric reports mean/p50/p90 and quality-gated ToolKB evidence,
or an explicit unavailable reason when no compatible PMU history exists.
Each group includes mean/p50/p90, units, backend/method, historical component
counts versus summary sample count, labeled histogram intervals, selected
context and composition assumptions. Unavailable targets include their reason
and configured edges. RSS statistics and histogram boundaries both display in
MiB; the API/trace continues to use bytes.

The following diagnostic section retains clause argv/index, legacy bucket and
ToolKB estimates, and every supplied LatticeKB time/resource algorithm, including
selected features and risk. These diagnostics are labeled separately from the
selected call prediction. Host SWE-Rebench tees the output to the terminal and
`agent-stdout.txt`; quiet mode suppresses this console output.

The API and trace use the versioned `pmu_prediction.v1` payload. PMU remains a
ToolKB-only whole-call prediction and does not affect placement or admission.
Offline reports always include PMU query, prediction-availability, and label
counts; datasets without PMU labels therefore remain explicit rather than
silently omitting the metrics.

### Restart-safe history loading

ToolKB and TrieKB snapshots now include additive `observed_counts` and
`legacy_counts` metadata. Replay reconciles observation multiplicities against
both pending and absorbed history; fresh online completions still append, and
equal measurements from distinct executions are retained. Timestamp identity
uses microsecond precision to tolerate JSON timestamp round trips. LatticeKB
continues to use its raw-observation multiset reconciliation.

Old aggregate snapshots lack execution identities. Their repository leaf
measurements are conservatively reconciled by primary metric multiplicity once
and then associated with replay identities. Public priors are not treated as
already learned repository history. This cannot reconstruct exact identities
from a partial old corpus, or remove duplicate weights already baked into an
old snapshot; rebuilding from the original history is required for that.
Frozen loading never merges history or mutates these counters.

For required host SWE-Rebench telemetry, a run containing `call_prediction`
must have a valid canonical prediction for every tool call and at least one
available target somewhere in the run. Individual unavailable targets are
allowed; old ToolKB CPU/memory diagnostics and clause buckets are not required
by this gate. Malformed or mixed incomplete canonical coverage fails even if
legacy diagnostics are available. Entirely legacy traces keep their previous
compatibility checks. Reports expose canonical presence, validity, availability
and per-target counts separately from the legacy diagnostic counters.
Frozen runs must report zero KB updates; explicit online runs must account for
their eligible updates. All collector, ownership and lifecycle checks still run.

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
