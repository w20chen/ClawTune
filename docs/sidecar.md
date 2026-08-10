# Scheduler Sidecar Reference

The sidecar receives OpenClaw lifecycle events, proxies model requests, owns
the eBPF collector, records traces, and serves recent measurements/predictions.

Related documents: [getting-started.md](getting-started.md),
[configuration.md](configuration.md), [architecture.md](architecture.md),
[trace-schema.md](trace-schema.md), and [legacy-eval.md](legacy-eval.md).

## Start the Sidecar

Normally, do not start the sidecar separately. The plugin starts it on demand
inside whichever OpenClaw runtime owns the work. With a Gateway, the same
sidecar is reused across sessions and runs until the Gateway exits:

```bash
# terminal 1
openclaw gateway run

# terminal 2
openclaw tui --session main
```

For a one-shot embedded run, plugin startup and cleanup follow that process:

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Run uname -a"
```

Start the sidecar explicitly only when OpenClaw is non-interactive and cannot
perform the required privilege prompt, or when a service manager deliberately
owns its lifetime:

```bash
python3 scripts/clawtune.py sidecar
```

All paths use `.env`, listen on `127.0.0.1:8765`, and require the kernel
privileges needed by eBPF. The plugin keeps `sidecarCommand` empty and resolves
the current checkout, `.venv`, matching kernel build tree, and `sudo` launch at
runtime. A pre-agent hook waits for an identified ClawTune health response to
eliminate the old first-request race.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Identified process liveness |
| `GET /health/ready` | Identified API readiness |
| `GET /metrics` | Prometheus metrics |
| `GET /v1/tools/recent` | Recent tool executions |
| `GET /v1/models` | OpenAI-compatible model discovery |
| `POST /v1/chat/completions` | Model proxy and tracing |

Both health responses include `service: clawtune-scheduler` and
`schema_version: scheduler.health.v1`. The launcher checks those fields instead
of treating any HTTP server on port 8765 as ClawTune. An unrelated listener is
therefore a port conflict and startup stops with an actionable error. The
public response contract is `contracts/health.schema.json`.

The health endpoints do not compile or attach probes on every request. Use
`python3 scripts/clawtune.py check` after a kernel, BCC, or Clang update to
verify the complete collector with a real process.

Quick checks:

```bash
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
curl -fsS http://127.0.0.1:8765/metrics
curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
```

## Resource Prediction

The built-in `tool_resource` predictor learns from valid command artifacts and
configured OpenClaw JSONL traces. It persists its knowledge under the artifact
directory so exact command, argument-prefix, executable, and global evidence
can survive restarts. When evidence is missing, the prediction remains unknown
rather than inventing a value.

Predictions cover command latency buckets plus empirical conditional estimates
for latency, CPU, and memory. They also include three clause-level point-time
predictors: `shrinkage`, `loso`, and `max_cardinality`. These algorithms share
one independent, flat lattice KB; common nodes and nodes carrying a repository
feature live in the same node map rather than separate public/repo layers.

The lattice learns only from eligible eBPF `ClauseObservation` values. Results
keep one independent prediction per exec-producing static clause: pipeline,
conditional, and sequential clause times are never combined into a synthetic
command duration. The lattice is prebuilt during cold start and updated as
clauses complete; large corpora are bounded deterministically. Field-level
details (including the `lattice_candidate_limit_exceeded` outcome) are
documented in [trace-schema.md](trace-schema.md).

### Lattice Time Prediction

Training normalizes each clause into a feature set $F$ (core `tool`/`target`
plus optional `repo`, `cwd`, `env_id`, flags, heredoc imports) and creates the
exact node $F$ plus every node $C = \mathrm{core} \cup S$ with $S$ any subset
of ≤ 6 optional features. Each node aggregates $m_C$ samples, median
$\mathrm{med}_C$, and log-variance $s_C^2$. A query $Q$ activates all nodes
$C \subseteq Q$; each algorithm predicts the median of the node it selects.

**`max_cardinality`** — select the most specific activated node
$C^* = \arg\max_{C \subseteq Q} |C|$; if none, fall back to the global median.

**`loso`** (Leave-One-Signature-Out) — score each activated node by

$$\mathrm{score}(C) = |C| - w\cdot R_C, \qquad w = 1.0,$$

where $R_C$ is the LOSO error over the $m$ distinct command signatures of $C$,
with $z_q = \log(1 + \mathrm{med}(q))$ for signature $q$:

$$R_C = \frac{1}{m}\sum_q\Big(z_q - \frac{1}{m-1}\sum_{q'\ne q} z_{q'}\Big)^2 .$$

Nodes with $m < m_{\min} = 2$ signatures are excluded (risk 999 for
single-signature nodes), except the most specific activated node, which is
rescued with loo_mse_log(C) — the leave-one-out MSE of
log-durations, or the global variance if $m_C = 1$. Predict from
$C^* = \arg\max_C \mathrm{score}(C)$.

**`shrinkage`** (Bayesian shrinkage + risk frontier) — shrink each node's
variance toward its immediate parents $P \subset C$:

$$R_C = \frac{(m_C - 1) \cdot s_C^2 + \kappa \cdot v_{\mathrm{par}}(C)}{(m_C - 1) + \kappa},
\qquad \kappa = 5,$$

where $v_{\mathrm{par}}(C) = \mathrm{median}\{R_P\}$ over parents; $m_C = 1$
collapses to $R_C = v_{\mathrm{par}}(C)$, and a top-level single-sample node is
a cold start with $R_C = \sigma^2_{\mathrm{global}}$. If the exact node $Q$
exists, return $\mathrm{med}_Q$ directly. Otherwise each activated node gets risk

$$\rho_C = R_C + \frac{\alpha}{\sqrt{m_C}}, \qquad \alpha = 0.03,$$

(doubled for cold starts). A dominance step drops $C$ when a more specific node
$D$ satisfies $C \subset D$ and $\rho_D \le \rho_C + \delta$ ($\delta = 0.15$);
from the surviving frontier, prefer the most specific node if its risk is within
$\tau = 0.5$ of the lowest-risk node, else take the lowest-risk node.

Offline benchmark results for these algorithms are in
[legacy_eval_final_report.md](legacy_eval_final_report.md) and are reproduced
with [legacy-eval.md](legacy-eval.md).

## Collection Boundaries

Managed `exec` calls are released only after the sidecar has prepared the
collector and cgroup scope. Native sandbox file tools can be correlated through
Docker events; when they share a container cgroup, the trace explicitly marks
the shared attribution boundary.

The collector is required by default. Disabling it is useful only to isolate an
unrelated API/plugin problem; the resulting resource data is incomplete.

ClawTune observes by default: scheduling/placement recommendations do not
forcibly move work in the current release.

## Service Manager Integration

For a persistent machine, wrap the same `sidecar` command in the site's service
manager and use the repository owner as the working user. There is no generic
service unit in the repository because working directories, account names, and
privilege policies are deployment-specific. The command itself invokes sudo,
so unattended operation requires a tightly scoped local policy for the
verified ClawTune launcher rather than a broad passwordless shell.

Keep the service bound to `127.0.0.1` unless authentication, firewalling, and
TLS termination have been designed for remote access. Provider credentials and
raw traces can contain sensitive data.

## Container-Only Development

`docker compose up --build scheduler` is useful for API development. It is not
the supported measurement deployment by itself: a container does not inherit
the host's matching headers, tracefs mount, perf access, and cgroup boundaries
simply because it is privileged.

## Configuration and Security

See [configuration](configuration.md) for normal settings. The complete
environment surface remains in `services/scheduler/src/agent_scheduler/config.py`
for developers.

- Bind locally unless remote authentication/TLS is deliberately configured.
- The proxy normally forwards OpenClaw's authorization header.
- Treat a custom sidecar shell command as trusted administrator input.
- Do not commit API keys or unredacted traces.
- Setup applies the plugin's `securityBoundaryAccepted: true` because the
  managed launcher rewrites shell execution.
- Keep the sidecar local and use `AGENT_SCHEDULER_TOKEN` if another local user
  must not call it.
- Never commit `.env`, model provider credentials, raw benchmark workspaces, or
  trace output.
- eBPF-disabled diagnostic output is incomplete and must not be presented as a
  successful ClawTune measurement.
