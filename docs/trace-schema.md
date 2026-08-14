# Trace & Protocol Reference

For operational guides see
[getting-started.md](getting-started.md),
[configuration.md](configuration.md), or
[../swe_rebench/README.md](../swe_rebench/README.md).

Public protocol schemas live in `contracts/`. eBPF exec-clause command artifacts
are described by `contracts/clause-telemetry.schema.json`. Validate:

```bash
python tools/validate_contracts.py
```

Main event families:

- `clawtune.v1` tool before/completed events
- model start/end events
- `clawtune.v2` managed execution registration and scope lookup
- schema trace records written as JSONL

## Trace Format

```text
traces/*.jsonl
```

SWE-Rebench traces are written under:

```text
swe_rebench/traces/<task_id>/*.jsonl
```

Deep Research Bench traces are written under the same schema under:

```text
deep_research_bench/.runtime/traces/<task_id>/*.jsonl
```

Deep Research Bench uses the sandbox-container / per-PID scope for its
read/edit/web tools and does not require exec-clause artifacts; the
records themselves are identical.

Inspect traces:

```bash
python tools/inspect_trace.py traces/<trace-file>.jsonl --all --details
python tools/inspect_trace.py traces/<trace-file>.jsonl --all --timeline
```

A successful instrumented run has a model span, a managed tool execution, an
attached cgroup/process scope, a finalized eBPF command artifact with
executable/argv data, and no collector loss. API health alone proves only that
the process is listening; `setup`/`check` prove kernel collection.

Expected record types (each record carries the current `schema_version` and
`trace_format_version`):

```json
{"record_type":"trace_metadata"}
{"record_type":"span_start","kind":"llm","name":"..."}
{"record_type":"span_end","kind":"tool","name":"exec"}
```

Useful fields:

- `duration_ns`: span duration from monotonic clock in nanoseconds (string).
- `duration_sec`: span duration in seconds, derived from `duration_ns` / 1e9 (string).
- `input.messages`: LLM request messages when proxy capture is active.
- `output.content`: LLM output. When the model emits tool calls, this may be
  an object containing both `content` and `tool_calls`.
- `input.requested_args`: tool input when `trace.include_raw_events: true`.
- `prediction`: tool prediction captured before execution on tool
  `span_start` records. This mirrors the `/v1/decisions/tool` response
  `prediction`, including native `tool_resource` details when available.
  `prediction.tool_resource.continuous_predictions` contains best-effort
  non-MLP `RuntimeToolResourceKB` conditional-p90 estimates for
  `latency_ms`, `peak_cpu_cores`, and `peak_memory_mb`; memory requires a
  pre-call ambient memory anchor and otherwise reports an unavailable note.
  `prediction.tool_resource.lattice_time_predictions` contains one record for
  each exec-producing static clause. Its `predictions` array reports the
  `shrinkage`, `loso`, and `max_cardinality` point estimates in milliseconds,
  together with selected-feature, evidence, risk, exact-match, fallback, or
  explicit unavailability metadata. All three algorithms read the same
  independent, flat lattice KB, which is trained only from eligible
  eBPF `ClauseObservation` latency measurements. For compound commands these
  remain per-clause results; the sidecar does not synthesize a command-level
  duration across sequential, conditional, or pipeline clauses.
  `prediction.tool_resource.prediction_algorithms` lists the enabled
  non-MLP predictors and records `tool_resource.mlp` as excluded.
- `resources.attribution_status`: resource attribution status.
- `resources.cpu_time_s`, `resources.rss_peak_bytes`: sampled resource data.
- `resources.monitor_duration_ns`, `resources.monitor_*_time_ns`, and
  `resources.cgroup_cpu_time_s`: monitor timing fields, with cgroup CPU time
  populated when the sampler source is cgroup v2.
- `resources.sampling_interval_ms`, `resources.sampling_point_count`,
  `resources.sampling_quality`: resource sampler cadence and quality.
- `resources.resource_timeline`: per-sample resource timeline, capped by
  `CLAWTUNE_RESOURCE_TIMELINE_MAX_POINTS`.

Coverage reasons distinguish attribution failures from expected shared scopes:

- `not_applicable`: no local payload process applies, e.g. LLM spans.
- `internal_tool_no_process`: an in-process tool had no PID/cgroup scope.
- `shared_runtime_process`: an internal tool was sampled through the shared
  OpenClaw runtime process, not a dedicated tool process.
- `shared_sandbox_container`: an internal tool was sampled through the shared
  OpenClaw Docker sandbox container cgroup, not a dedicated tool process.
- Native tools with `execution.source: docker-events` and
  `resources.attribution_source: docker-exec-pid` use the matched Docker exec
  host PID and descendants. Their resource scope is `process_tree`, because a
  Docker exec does not receive a dedicated cgroup.
- `monitor_window_no_overlap`: a PID/cgroup existed, but the sampler did not
  capture an overlapping resource window.

`coverage_ratio` measures overlap with the complete tool span, not just the
payload command. In fork-exec mode the payload remains behind its pipe gate
while `/started` resolves the host PID and attaches collectors. That pre-monitor
registration window can lower span coverage without implying that the same
duration of payload work was missed; it still means the complete tool span was
not monitored from its initial boundary.

For complete cgroup sampling in SWE-Rebench, each managed execution must enter
its own cgroup. The container runtime uses the privileged/cgroup-v2 settings in
`swe_rebench/config.yaml`. Host-OpenClaw's Docker sandbox does not consume those
runner-owned Docker flags, so its fork-exec launcher requests a privileged
host-side cgroup gate when the sandbox cgroupfs is read-only. With
`cgroup_required=true`, launcher startup fails unless the resulting scope is
`exclusive-execution-cgroup`; the runner also rejects a shared
`docker-<container>.scope` or a cgroup path reused by multiple executions.
The sidecar accepts an exclusive scope only after `cpu` and `memory` are
confirmed in the execution root's `cgroup.subtree_control` and the new leaf's
accounting files are readable. At completion, the launcher-authenticated
execution scope takes precedence over OpenClaw's shared sandbox/runtime scope.
A sidecar-owned leaf remains present through the final resource snapshot and
trace flush, then is removed; delayed garbage collection covers a lost
completion hook.

The JSON Schema contracts remain the source of truth for protocol details.
