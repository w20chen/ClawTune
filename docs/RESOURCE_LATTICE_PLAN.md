# CPU and memory lattice prediction plan

Status: superseded planning discussion. The user subsequently chose raw metric storage
and query-time thresholds. See [implemented design](lattice-resources.md); the fixed
heavy-score targets below are not implemented.

## Design decision

Generalize the existing lattice adapter into a reusable scalar-target engine.
Keep LatticeTimeKB as a compatibility facade, with unchanged latency outputs,
normalization, algorithm order, numerical behavior, and old snapshot reader.
CPU and memory use the same feature-subset matching, bounded node generation,
median estimation, shrinkage/LOSO/max-cardinality selection, causal buffering,
preparation, locking, and persistence lifecycle. Each target has independent
sample eligibility, node statistics, variances, selected context, and risk.
Do not reuse the latency-selected node blindly for resource predictions.

The existing shrinkage algorithm estimates variance; it does not estimate
heavy probabilities. Preserve that distinction in names and documentation.
Primary resource decision algorithm: shrinkage. Expose all three algorithms
for comparison, without voting or silently switching the primary algorithm.

## Versioned definitions

Definition heavy-v1, thresholds inclusive:

- Memory heavy: peak attributed memory >= 536870912 bytes (512 MiB).
- CPU heavy: total CPU seconds >= 1.0 AND average CPU cores >= 0.8.
- Average CPU cores = total CPU seconds / wall seconds, including descendants
  in the declared attribution scope; wall seconds must be positive.
- CPU seconds are core-seconds, not elapsed seconds or host CPU percentage.
- The two labels are independent. Memory heavy means footprint, not bandwidth.
- Machine load and currently available capacity do not change these labels.
  Capacity/placement advice remains a separate advisory concern.

Store raw CPU seconds, wall seconds, and memory bytes. Derive resource lattice
targets from each individual observation, before computing any statistics:

    cpu_heavy_score = min(cpu_seconds / 1.0, avg_cpu_cores / 0.8)
    memory_heavy_score = peak_memory_bytes / 536870912

For either resource, score >= 1 is exactly its heavy definition. The CPU score
preserves the joint event: do not multiply marginal probabilities or combine
independently predicted CPU and duration medians. It is a dimensionless
threshold margin, not utilization or a probability. Return its median estimate;
memory score can also be converted back to a median byte estimate. Preserve
CPU seconds/average cores in observations for audit, without adding more
independent regression models in v1.

Nonnegative zero scores are valid. The generic numerical engine must support
them and log1p(score); keep positive-only latency validation in its facade.
Use dimensionless scores so byte units do not distort log-space risk tuning.

## Observation scope and telemetry

Two explicit scopes share the engine, never their measurement pools:

1. tool_call: authoritative top-level heavy label; start with managed exec and
   verified exclusive per-execution cgroups. CPU comes from cumulative usage
   delta. Memory uses execution-lifetime cgroup peak (including cgroup-accounted
   cache), or sampled memory.current when a kernel peak is unavailable.
2. clause: per-static-clause diagnostic predictions using existing eBPF owned
   lineage CPU cumulative time, clause wall time, and aligned distinct-mm peak
   RSS. Its memory metric is rss_peak_bytes, not cgroup peak bytes.

Record scope, metric, measurement method, coverage, environment identity,
execution identity, and observation timestamps. Never silently import RSS into
a cgroup-memory target. Never derive CPU average from peak_cpu_cores: existing
call and clause paths use that legacy name for different quantities.

Do not sum process RSS high-water marks. Do not subtract an unrelated shared
container baseline. Lifetime memory.peak is valid only for a fresh exclusive
execution scope or an explicitly supported correctly reset measurement window.
Kernel feature detection is required; sampled peaks identify a weaker method
and cannot be presented as exact peak ground truth. Definitive negative memory
training/evaluation requires adequate coverage; sampled-only negative labels
remain approximate and are reported separately from authoritative evaluation.

Each target may be unavailable independently. Shared scopes, incomplete
attribution, missing metrics, and nonfinite/invalid values never become zeros.
Record telemetry even for unsuccessful calls. A normal nonzero exit with a
complete measurement is eligible. Interrupted/OOM/timeout runs are censored:
retain measurements and observed threshold-crossing evidence, but do not train
their lower bounds as complete continuous scores or negative labels in v1.
Report their coverage and false-negative risk separately. CPU's joint condition
cannot in general be established for an unfinished natural runtime.

## Feature and compound-command policy

Reuse existing clause normalization, with resource-specific adaptations outside
vendored source where possible. Do not inherit latency-specific exclusions of
pipe consumers as an assertion that those tools use negligible resources.

For resource nodes, scope/metric and a compatible environment class are hard
partitions. Initial environment class includes architecture and effective CPU
quota/cpuset; capture image identity when available. Unknown environment is an
explicit class. Repo remains optional as in the existing mixed lattice.
Exact identity retains ordered argv and relevant script/inline-code identity;
coarser feature matches are not reported as exact command matches.

Resource optional features initially cover repo, operation/target, parallelism,
and supplied input-size bucket. Only bounded stat calls for explicit regular
input files in the execution namespace may add size; no recursive scans or
pre-execution of the workload. Missing metadata has an explicit unknown value.
Retain existing node/candidate budgets, with a total resource preparation budget.

For whole compound calls, preserve ordered clause structure and shell operators
in full-call identity. Do not run the existing dominant-subcommand normalizer
on the whole call and attribute its resource label to one selected executable.
Use exact structural identity plus coarse compound-kind features; first-seen
compound calls may be unknown at tool level while clauses have predictions.
Never OR clause CPU labels into a call CPU label, add clause memory peaks, or
derive call probabilities under an independence assumption. Whole-call history
supplies the call prediction. Native/shared tools remain explicit unknown until
reliable call attribution is available.

## Probability and decisions

For each resource and algorithm, select its node with that resource's continuous
score statistics. In that exact selected node, let n be the eligible complete
observations and h the number whose score is >= 1. Return the simple smoothed
frequency (h + 1) / (n + 2), method beta_1_1_frequency. This is an uncalibrated
initial probability estimate, not selected_risk or a confidence percentage.

Default decision rules: require n >= 10 and a compatible non-global context;
p >= 0.8 => heavy; p <= 0.2 => not_heavy; otherwise unknown. No eligible evidence
means null score/probability. Small sample nodes may expose numeric diagnostics
but their state is unknown with insufficient_evidence. Do not hide an exact
small-sample match by silently relabeling it as well supported.

These probability rules are intentionally separate from the existing variance
shrinkage and are evaluated before any claim of calibration. Thresholds and
support gate are configuration with a persisted configuration fingerprint.
Changing heavy thresholds rebuilds resource scores from raw observations.

## Code and protocol boundaries

- New generic adapter package: services/sidecar/src/prediction_lattice/.
  Isolate scalar node building, selection, target specifications, and buffering.
  Avoid presenting resource scores through public duration_s/prediction_ms names.
- Preserve tool_time/lattice_kb.py API and old snapshots with a latency facade.
  Keep the vendored algorithm implementation and provenance intact when feasible;
  any required generic numerical adaptation gets regression coverage.
- New resource integration: clawtune_sidecar/predictors/resource_lattice.py.
  Wire preparation, query, completion, and atomic persistence into existing
  predictor lifecycle. No OpenClaw core or trace dataset changes.
- Keep services/sidecar/src/tool_resource unchanged unless a demonstrated
  telemetry gap requires a minimal separately explained change.
- JSON Schema first: add tool_resource.resource_lattice with definition version,
  config fingerprint, primary algorithm, tool_call predictions, and clauses
  keyed by clause_index. Each scope declares metric/method and per-algorithm
  CPU/memory score, probability, state, selected_features, evidence_count,
  selected_risk, exact_match, fallback, and unavailable reason.
- Retain existing lattice_time_predictions and legacy continuous_predictions.
  New heavy decisions use only resource_lattice, never legacy peak_cpu_cores.
- New versioned resource-lattice-kb.json stores raw observations and eligibility,
  not just derived labels. Keep the old time snapshot readable. No invented
  memory seed data; historical imports require provable units/scope/coverage.
- Deduplicate new records by stable execution identity plus scope/clause and
  metric version. Preserve genuine repeated executions. Observation visibility
  requires ts_end < query_ts; reload and startup import must preserve that rule.
  Freeze one feature snapshot and knowledge generation for each before-call
  prediction; concurrent completions cannot partly update its target outputs.

## Implementation sequence and acceptance

1. Lock schemas, target units, eligibility, normalization and fixtures.
2. Extract the generic engine; prove existing three latency algorithms and
   snapshots unchanged on deterministic fixtures.
3. Implement resource target adapters, scope-safe observations, compound
   identity, score/probability output, and snapshot replay.
4. Wire before-call prediction and completion learning, then plugin trace types
   and user-facing diagnostics. Keep preparation off the prediction hot path;
   publish prepared generations atomically and enforce bounded query work.
5. Evaluate on Linux real executions and causal chronological replay.

Tests: inclusive boundaries (512 MiB, 1 CPU second, 0.8 cores), zero/missing
values, mixed CPU/memory labels, joint CPU score versus misleading marginal
medians, independent eligibility, fresh versus reused cgroups, sampled peaks,
shared attribution rejection, unsuccessful/censored runs, native tools,
compound/pipeline scope separation, environment mismatch, small-sample unknown,
all three algorithms, latency equivalence, causal overlap/equal timestamps,
snapshot reload/deduplication, and concurrent prediction/update consistency.

Evaluation: sequential predict-then-observe, no future-trained startup snapshot;
separate held-out repositories and environments. Report heavy precision, recall,
PR-AUC, Brier score/reliability, decisive coverage and unknown rate per resource,
scope, cold/warm start, and measurement method. Recall includes heavy calls left
unknown as unrecognized; also report conditional metrics on decisive cases.
Compare with constant-base-rate and simple command-group empirical baselines.
Do not claim success from accuracy alone or manufacture a numerical accuracy
target before examining class balance and authoritative memory coverage.

Performance acceptance: incremental before-call p95 budget 10 ms on the target
Linux host at the existing default node budgets; measure and report preparation
CPU/memory separately. Budget overrun returns explicit unknown, not a late
post-execution prediction. This is a proposed target, not a measured result.

Validation commands when implementation exists:

    python tools/validate_contracts.py
    python -m pytest tests -q --basetemp .pytest-tmp-root
    python -m pytest services/sidecar/tests -q --basetemp .pytest-tmp-sidecar
    npm test                 # cwd packages/clawtune-plugin
    npm run typecheck        # cwd packages/clawtune-plugin

Add Linux telemetry/integration workload commands with implementation. Document
every validation command that cannot run in docs/CURRENT_PLAN.md. No tests were
attempted for this documentation-only planning step; no runtime accuracy or
performance result is claimed.
