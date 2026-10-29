# ClawTune: Monitoring and Predicting Agent Tool Resources

## 1. System design

ClawTune provides a Python library, a local monitoring service, evaluation programs, and an OpenClaw integration plugin. The plugin correlates model requests and tool calls; the service collects execution measurements and predicts subsequent calls from historical evidence. It does not modify OpenClaw core. Placement advice is advisory.

Docker supplies task environments, cgroups bound resource accounting, eBPF attributes executable clauses to processes, and perf supplies hardware counters. Daily operation learns continuously; online benchmarks share run-local history; the standard offline evaluation freezes models during testing.

## 2. Measurement scope and validity

Raw tool-span duration includes the tool lifecycle and wrapper overhead. Shell prediction targets the executable subcommands included in the workload model, called clauses below. ToolKB learns the corresponding whole-call duration, while the other models reconstruct it from those clauses. Native tools use their call-level duration. These measurement scopes are not interchangeable.

The [measurement reference](tool-profile.md#quick-guide-measurements-and-predictions) defines duration, cumulative and peak CPU, sampled process RSS, environment total/extra memory, and the nine ToolKB PMU targets. Environment memory includes background charges; extra memory is a high-water delta, not exclusive process attribution. Memory sources remain separate, and host VM RSS cannot substitute for guest memory.

Training retains approximate observations, including native-tool shared-runtime values, and zero-fills missing targets from short or insufficient sampling. Interrupted executions retain their observed prefix; a confirmed effective timeout supplies a duration lower bound. Raw ownership, collector quality and interruption metadata remain visible, and per-target provenance distinguishes measured, approximate, partial and imputed labels. These labels improve coverage but must not be described as exclusively attributed measurements or exact completed-execution truth. Undefined hardware ratios use the same explicit missing-target policy rather than division by zero.

## 3. Historical evidence and empirical predictions

The implementation maintains three independent models; they do not fill missing evidence from one another:

| Index | Description | Observation scope |
| --- | --- | --- |
| ToolKB | Call-level history | Whole-call training values, including workload duration and hardware counters |
| TrieKB | Clause-prefix index | Executable clauses and ordered argument prefixes |
| LatticeKB | Feature-subset index | Clause contexts ordered by feature-set inclusion |

Each model searches its own history. ToolKB and TrieKB use the first nonempty
match for each target, even if it has only one sample. Zero-filled values count
as samples. Different targets can therefore use different matching levels.

| Model | Matching and fallback |
| --- | --- |
| ToolKB | Repository-specific exact command, then command prefixes of up to four tokens, then a compatible executable. Next: a normalized whole-command match across repositories, a compatible executable, the outer tool name (such as `exec`), and finally all calls. Without a command, it uses repository/tool name, tool name across repositories, then all calls. |
| TrieKB | Repository-specific exact subcommand, then argument prefixes of up to four tokens, then a compatible executable. Next: a normalized command match across repositories, a compatible executable, and finally all subcommand observations. |
| LatticeKB | An exact feature set uses that set's own observations. Otherwise it selects one matching subset that retains the executable and entry identity, using the selection method below. It can omit repository and other optional features, but has no unrestricted global fallback. |

For single commands, compatible prefixes retain known module or script identities;
`python -m pytest` does not use a generic Python prefix. ToolKB can still reach
its broader tool-name or global fallback. A compound-call prefix describes the
whole call, including later commands; it is not a measurement of the first
subcommand. See [command parsing](command-parsing.md) for syntax coverage.

An empty search produces an unavailable target. Lattice's default subset search
also returns unavailable when its non-exact query exceeds 512 candidates.
Memory matching keeps measurement sources separate, and unsupported shell
structures can prevent a complete call prediction even when some subcommand
predictions exist.

For ToolKB, TrieKB, and LatticeKB, given the selected valid samples $y_1,\ldots,y_n$ for a target, the empirical distribution and mean are

$$
\widehat F(y)=\frac{1}{n}\sum_{i=1}^{n}\mathbf{1}[y_i\le y],
\qquad
\bar y=\frac{1}{n}\sum_{i=1}^{n}y_i.
$$

For average CPU, samples are per-observation CPU-time/duration ratios; their mean is not the ratio of separate means. Outputs include the mean, median, empirical p90, and a histogram. The p90 is the ordered sample at rank $\lceil0.9n\rceil$; an even-sized sample median averages the two central values. Histogram intervals are left-closed and right-open, and probabilities are sample proportions. Neither sample count nor histogram mass is calibrated confidence.

## 4. Feature-subset prediction

### 4.1 Context construction

Normalize each executable clause into a feature set $F(x)$ containing its executable, target, options, and available project identity. A context $S$ aggregates historical clause observations satisfying $S\subseteq F(x_i)$. Let $C(x)$ contain the query's required executable and entry features. The matching contexts for query $x$ are

$$
\mathcal A(x)=\{S:C(x)\subseteq S\subseteq F(x),\ n_S>0\}.
$$

More features make a context more specific, generally reducing its sample size. Fewer features broaden coverage but can mix different workloads. Context selection therefore trades specificity against statistical stability.

The implementation retains complete feature sets while bounding partial combinations. It maintains a bounded subset partial order rather than explicitly materializing a full power set. The normalizer treats `repo` as optional, so nodes with and without project identity coexist. It is not a hard isolation boundary. LatticeKB groups training samples and selects contexts independently per target. Offline identity fallback is described in the [benchmark guide](benchmarks.md#5-fixed-trace-offline-evaluation).

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

The penalty for a top-level singleton is $2\alpha$. The active prediction path disables time-drift penalties. A zero stored shrinkage variance falls back to the local or global variance. The active path uses $\kappa=0.5$ and $\alpha=0.03$.

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

ToolKB uses call-level evidence. TrieKB and LatticeKB (shrinkage by default) reconstruct retained workloads from their own clause evidence.

For supported foreground shell commands, unconditional serial lists, and simple pipelines, the composer independently samples clause distributions. Let $g$ index serial groups and $j$ index clauses within a pipeline group:

$$
T^{(b)}=\sum_g\max_{j\in g}T_j^{(b)}.
$$

A standalone clause forms a one-element group. A fixed random seed generates 2048 draws, from which summary statistics are computed. Clause medians or p90s are not added directly, and generated draws are not counted as historical observations.

This approximation assumes that foreground clauses cover the workload, ignores shell and hook overhead, and treats clause distributions as independent. `exec` and `terminal_exec` share shell extraction. `cd`, assignments, `env`, `timeout`, and `nohup` retain executable-stage predictions. For `&&` and `||`, the current composer includes every retained stage and annotates its success/failure condition; it does not forecast exit status or branch probability. Loops, substitutions, and background jobs cannot establish a complete-call composition. CPU time sums across retained stages. Average CPU divides sampled total CPU time by the sampled composed duration: serial durations sum and pipeline durations take their maximum. Sequential CPU and RSS peaks use their maximum; parallel peaks use a conservative sum. Those peak rules do not preserve temporal alignment, and their p90 is not calibrated simultaneous-load coverage. Environment total and extra memory use the maximum sampled clause prediction as a call-level approximation. For a single clause, environment total assumes no higher peak outside the clause, and extra additionally assumes that the clause baseline equals the tool baseline. The downstream consumers `cat comm column cut egrep fgrep fold grep head hexdump less more nl od paste rev rg sed tac tail tee tr ts uniq wc xxd` are excluded at pipeline positions greater than zero, for every clause target and ToolKB workload-duration labels. They remain eligible alone or first in a pipeline; other eligible call-level metrics are unaffected. Individual retained-clause predictions remain available. A single-clause shell command also uses the composition path when its estimate is reconstructed from clause evidence.

## 6. Learning, initialization, and evaluation

An online query may use an observation only when $t_i^{end}<t_q^{start}$. Concurrent tasks share evidence in actual completion order; fixed task selection does not ensure identical learning interleavings. Standard offline testing freezes all models and cannot incorporate test outcomes.

Default seed contents, snapshot compatibility, and rebuild instructions are maintained in [Output and persistent state](getting-started.md#3-output-and-persistent-state). The bundled default includes its former test samples and is intended for deployment; those samples cannot provide held-out accuracy for that bundle. Training also includes non-shell tools, while standard offline predictions and accuracy denominators include only `exec` and `terminal_exec`. Source workloads and hardware conditions limit transfer to other environments.

Online execution evaluates collection and continuous learning behavior. Fixed-trace evaluation measures prediction error outside the training subset. The current offline protocol keeps each task and all its attempts on one side of a deterministic split, stratified by benchmark and repository, category, or dataset fallback. Singleton groups are training-only. The resulting overall fraction can differ from the requested fraction, and within-project tests do not establish unseen-project performance.

Evaluation should report availability alongside absolute error, task-level averages, and baseline differences. Quantiles require both empirical coverage and pinball loss:

$$
\ell_\tau(y,\widehat q)=(\tau-\mathbf1[y<\widehat q])(y-\widehat q).
$$

Compare methods on the same labeled calls, separating measured and imputed values, and select hyperparameters within the training data. Histogram accuracy depends on boundaries and class balance and cannot alone establish better duration prediction. Hardware quotas, input sizes, sampling error, correlated commands, and platform changes limit applicability.

Commands and output interpretation are centralized in the [benchmark guide](benchmarks.md). [JSON Schemas](../contracts/) define protocol fields. Algorithm provenance and licensing are recorded in the [vendoring notice](../services/sidecar/src/tool_time/_lattice_vendor/VENDORED.md).
