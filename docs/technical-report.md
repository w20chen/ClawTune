# ClawTune: Monitoring and Predicting Agent Tool Resources

## Abstract

Agent tool calls vary substantially in execution time and resource consumption. Tool names and static resource limits alone provide an incomplete description of this variation. ClawTune combines call tracing, operating-system measurements, and empirical prediction to estimate execution time, CPU use, and memory consumption before a call, then update its statistical models after execution. This report describes the system architecture, measurement model, prediction methods, and evaluation protocol.

## 1. System design

The system comprises an OpenClaw plugin, a local monitoring service, and evaluation programs. The plugin associates model requests with tool calls. The service proxies model requests, collects execution measurements, and maintains historical statistics. Evaluation programs organize tasks, isolate execution environments, and score predictions.

```text
OpenClaw -- plugin -- local service -- historical statistics and prediction
               |            |
               |            +-- model proxy and tracing
               +-- tool execution -- cgroup / eBPF / perf
```

Docker supplies tool execution environments. Linux cgroups establish resource accounting boundaries, eBPF associates executable clauses with their descendant processes, and perf hardware counters supply microarchitectural measurements. Correlated events form execution traces for subsequent analysis.

Daily operation learns continuously. Online benchmarks share learning within each run. Offline evaluation trains on a fixed subset and freezes all models during testing. The system provides concurrency admission information and resource recommendations for deployment components.

## 2. Measurement scope and validity

A tool call may contain a shell command, represented as text such as `grep pattern file | head`. Parsing that shell command can produce one or more executable clauses: in this example, the producer and the pipeline consumer are separate clauses. Call-level duration covers the complete tool lifecycle, including wrapper overhead. Clause-level duration covers the execution interval attributed to one executable clause. These quantities are not interchangeable.

| Target | Definition | Unit |
| --- | --- | --- |
| Duration | Wall-clock time within the stated observation boundary | ms |
| CPU time | Cumulative CPU time of the attributed workload | core-s |
| Average CPU use | CPU time divided by duration for the same observation | cores |
| Peak CPU use | Maximum CPU use over fixed 500 ms windows | cores |
| `memory_total_peak_bytes` | Sampled peak environment memory, including its background | bytes |
| `memory_extra_peak_bytes` | `max(0, memory_total_peak_bytes - memory_baseline_bytes)` | bytes |

For observation $i$, average CPU use is $a_i=c_i/t_i$. Its predicted mean averages these per-observation ratios, rather than dividing the separate means of CPU time and duration. Process RSS is retained only as a diagnostic, not as a memory prediction target. Current collection samples cgroup v2 `memory.current`: the background is memory already charged to that cgroup before the call, including its existing processes and charged cache; it is not the whole host OS. Guest `MemTotal - MemAvailable` is a separate measurement source for future VM integration. All three KBs keep the sources separate; host VM RSS is not guest memory. Total and extra are sampled estimates, not guaranteed allocation limits.

Resource labels require identifiable execution ownership and sufficient collection quality for the target. Shared-container totals cannot serve as individual tool labels. Timeout and cancellation observations are censored and excluded from complete-execution labels. Missing values remain distinct from valid zeros, and each target has its own validity mask.

The downstream list is `cat comm column cut egrep fgrep fold grep head hexdump less more nl od paste rev rg tac tail tee tr ts uniq wc xxd`. These consumers at pipeline position greater than zero are excluded from independent clause modeling because their duration depends on upstream input. The same executables remain eligible when run independently or at the start of a pipeline.

Hardware profiling records cycles $C$, instructions $I$, last-level-cache read accesses $A$, and read misses $M$:

$$
\mathrm{IPC}=I/C,\qquad
\mathrm{MPKI}=1000M/I,\qquad
\mathrm{MissRate}=M/A.
$$

ToolKB stores all nine metrics: `cycles`, `instructions`, `llc_read_accesses`, `llc_read_misses`, `ipc`, `llc_mpki`, `llc_miss_rate`, `llc_read_accesses_per_cpu_second`, and `llc_read_misses_per_cpu_second`. The final two divide their event counts by that event's inherited perf `time_running_ns / 1e9`; they describe on-CPU read intensity, not DRAM bandwidth or bytes per wall second.

A zero denominator makes the corresponding ratio unavailable. Only complete, correctly attributed observations with consistent event semantics and no counter multiplexing enter prediction history. Scaled or incomplete counters remain diagnostic. Shared chip-level cache counters are not substituted for task-attributed measurements.

## 3. Historical evidence and empirical predictions

The implementation maintains three complementary statistical indexes:

| Index | Description | Observation scope |
| --- | --- | --- |
| ToolKB | Call-level history | Complete tool calls, including eligible hardware-counter metrics |
| TrieKB | Clause-prefix index | Executable clauses and ordered argument prefixes |
| LatticeKB | Feature-subset index | Clause contexts ordered by feature-set inclusion |

Whole-call history first retrieves a project's exact normalized call representation, then shorter prefixes, executable identity, or an applicable tool category. The clause-prefix index similarly backs off from an exact clause to argument prefixes and executable identity. When local evidence is absent, compatible public priors provide coarser executable- or tool-level evidence. Current call-level predictions do not fill missing targets from unrelated global samples.

Given the selected valid samples $y_1,\ldots,y_n$ for a target, the empirical distribution and mean are

$$
\widehat F(y)=\frac{1}{n}\sum_{i=1}^{n}\mathbf{1}[y_i\le y],
\qquad
\bar y=\frac{1}{n}\sum_{i=1}^{n}y_i.
$$

Outputs include the mean, median, empirical p90, and a histogram. The p90 is the ordered sample at rank $\lceil0.9n\rceil$; an even-sized sample median averages the two central values. Histogram intervals are left-closed and right-open, and probabilities are sample proportions. Neither sample count nor histogram mass is calibrated confidence.

## 4. Feature-subset prediction

### 4.1 Context construction

Normalize each executable clause into a feature set $F(x)$ containing its executable, target, options, and available project identity. A context $S$ aggregates historical clause observations satisfying $S\subseteq F(x_i)$. The active contexts for query $x$ are

$$
\mathcal A(x)=\{S:S\subseteq F(x),\ n_S>0\}.
$$

More features make a context more specific, generally reducing its sample size. Fewer features broaden coverage but can mix different workloads. Context selection therefore trades specificity against statistical stability.

The implementation retains complete feature sets while bounding partial combinations. It maintains a bounded subset partial order rather than explicitly materializing a full power set. Project identity can be omitted to construct both project-specific and shared contexts. Each target builds its own eligible sample views and selects its context independently.

### 4.2 Variance shrinkage

Transform observations as $z_i=\log(1+u_i)$, where $u_i=y_i/b$ is dimensionless. The scale $b$ is one second for duration, one MiB for memory, one core-second for CPU time, and one core for CPU-use targets. Unit conversion must preserve this scale.

For context $S$, let $n_S$ denote its sample count, $s_S^2$ its log-space sample variance, and $v_P$ the median variance of its nearest available more-general parent contexts. The shrinkage estimate is

$$
v_S=\frac{(n_S-1)s_S^2+\kappa v_P}{n_S-1+\kappa}.
$$

A singleton inherits its parent variance. Without usable parent evidence, a multi-sample context uses its own variance; a singleton uses global variance and receives an additional small-sample penalty. For ordinary contexts, selection risk is

$$
r_S=v_S+\frac{\alpha}{\sqrt{n_S}}.
$$

The penalty for a top-level singleton is $2\alpha$. The active prediction path disables time-drift penalties. A zero stored shrinkage variance falls back to the local or global variance. Defaults are $\kappa=5$ and $\alpha=0.03$.

A more-specific context $T$ eliminates $S$ from selection when

$$
S\subset T,\qquad r_T\le r_S+\delta.
$$

Among the remaining contexts, selection starts with minimum risk, but prefers the most specific candidate if its excess risk is within tolerance $\varepsilon$. Current values are $\delta=0.15$ and $\varepsilon=0.5$.

An exact full-feature match bypasses risk comparison and directly supplies the samples. The final duration point estimate is the selected sample median: shrinkage stabilizes variance and context selection, not the predicted mean itself. An exact singleton match is not a reliability guarantee.

### 4.3 Leave-one-signature-out selection

Leave-one-signature-out (LOSO) evaluation groups observations by their complete normalized feature sets, treating repeated executions of one clause type as a group. For the $m$ types covered by a context, define

$$
z_q=\log\left(1+\text{median}_{i\in q}(u_i)\right).
$$

Hold out each type and predict its value using the mean of the remaining type values:

$$
\widehat z_{-q}=\frac{1}{m-1}\sum_{j\ne q}z_j,
\qquad
L_S=\frac{1}{m}\sum_q(z_q-\widehat z_{-q})^2.
$$

The selector balances feature count and this cross-type error:

$$
S^*=\arg\max_{S\in\mathcal A(x)}\left(|S|-\lambda L_S\right),
\qquad \lambda=1.
$$

For a single-type exact match with repeated observations, per-observation leave-one-out error replaces LOSO error. Other single-type cases receive a fixed large penalty. Final predictions still use the selected context's empirical samples; the validation mean above is not the final point predictor.

### 4.4 Maximum feature matching

The comparison method selects the matching context with the most features:

$$
S^*=\arg\max_{S\in\mathcal A(x)}|S|.
$$

Ties prefer more observations. This specificity baseline has no cross-validation risk term. Resource targets with no matching context are unavailable. Historical whole-call duration diagnostics retain broader fallbacks, which do not establish compatible call-level resource evidence.

## 5. From commands to tool calls

For each target, the system prefers compatible complete-call evidence, followed by clause-prefix estimates and feature-subset estimates. The latter use shrinkage by default.

For supported foreground shell commands, unconditional serial lists, and simple pipelines, the composer independently samples clause distributions. Let $g$ index serial groups and $j$ index clauses within a pipeline group:

$$
T^{(b)}=\sum_g\max_{j\in g}T_j^{(b)}.
$$

A standalone clause forms a one-element group. A fixed random seed generates 2048 draws, from which summary statistics are computed. Clause medians or p90s are not added directly, and generated draws are not counted as historical observations.

This approximation assumes that foreground clauses cover the workload, ignores shell and hook overhead, and treats clause durations as independent. `exec` and `terminal_exec` share shell extraction. `cd`, assignments, `env`, `timeout`, and `nohup` retain executable-stage predictions. `&&` and `||` predictions state the assumed branch; they do not forecast exit status. Loops, substitutions, and background jobs cannot establish a complete-call composition. CPU time sums across retained stages; sequential CPU peaks use their maximum, and parallel peaks use a conservative sum. The latter is not a calibrated p90 of simultaneous load. Multi-clause CPU averages need paired duration/CPU samples; environment memory needs a joint baseline and timeline, so these whole-call estimates remain unavailable when that evidence is missing. Individual clause predictions remain available. A single-clause shell command also uses the composition path when its estimate is reconstructed from clause evidence.

## 6. Learning, initialization, and evaluation

An online query may use an observation only when $t_i^{end}<t_q^{start}$. Concurrent tasks share evidence in actual completion order; fixed task selection does not ensure identical learning interleavings. Offline testing freezes all models and cannot incorporate test outcomes.

The bundled initialization prior contains a small set of historical executable-clause observations with source identities removed and resource labels filtered. Call-level history starts empty. Runtime KB v3, TrieKB v6, and LatticeKB v3 reject earlier snapshots; the release prior contains no formal memory labels synthesized from historical RSS. The bundle is retained as a runtime resource; its source workload and hardware conditions limit cross-platform interpretation.

Online execution evaluates collection and continuous learning behavior. Fixed-trace evaluation measures prediction error outside the training subset. The current offline protocol keeps each task and all its attempts on one side of a deterministic split, stratified by benchmark and project or category. Singleton groups are training-only. The resulting overall fraction can differ from the requested fraction, and within-project tests do not establish unseen-project performance.

Evaluation should report availability alongside absolute error, task-level averages, and baseline differences. Quantiles require both empirical coverage and pinball loss:

$$
\ell_\tau(y,\widehat q)=(\tau-\mathbf1[y<\widehat q])(y-\widehat q).
$$

Compare methods on the same eligible labels and select hyperparameters within the training data. Histogram accuracy depends on boundaries and class balance and cannot alone establish better duration prediction. Hardware quotas, input sizes, sampling error, correlated commands, and platform changes limit applicability.

Commands and output interpretation are centralized in the [benchmark guide](benchmarks.md). [JSON Schemas](../contracts/) define protocol fields. Algorithm provenance and licensing are recorded in the [vendoring notice](../services/sidecar/src/tool_time/_lattice_vendor/VENDORED.md).
