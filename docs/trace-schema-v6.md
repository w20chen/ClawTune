# Trace Output

Traces are JSONL files. With `.env.example`, they are written under:

```text
data/traces/*.jsonl
```

SWE-Rebench traces are written under:

```text
swe_rebench/traces/<task_id>/*.jsonl
```

Inspect traces:

```bash
python tools/inspect_trace.py data/traces/<trace-file>.jsonl --all --details
python tools/inspect_trace.py data/traces/<trace-file>.jsonl --all --timeline
```

Expected record types:

```json
{"schema_version":6,"record_type":"trace_metadata","trace_format_version":6}
{"schema_version":6,"record_type":"span_start","kind":"llm","name":"..."}
{"schema_version":6,"record_type":"span_end","kind":"tool","name":"exec"}
```

Useful fields:

- `input.messages`: LLM request messages when proxy capture is active.
- `output.content`: LLM output. When the model emits tool calls, this may be
  an object containing both `content` and `tool_calls`.
- `input.requested_args`: tool input when `recordRawTrace: true`.
- `prediction`: tool prediction captured before execution on tool
  `span_start` records. This mirrors the `/v1/decisions/tool` response
  `prediction`, including native `tool_resource` details when available.
  `prediction.tool_resource.continuous_predictions` contains best-effort
  non-MLP `RuntimeToolResourceKB` conditional-p90 estimates for
  `latency_ms`, `peak_cpu_cores`, and `peak_memory_mb`; memory requires a
  pre-call ambient memory anchor and otherwise reports an unavailable note.
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
  `AGENT_SCHEDULER_RESOURCE_TIMELINE_MAX_POINTS`.

Coverage reasons distinguish attribution failures from expected shared scopes:

- `not_applicable`: no local payload process applies, e.g. LLM spans.
- `internal_tool_no_process`: an in-process tool had no PID/cgroup scope.
- `shared_runtime_process`: an internal tool was sampled through the shared
  OpenClaw runtime process, not a dedicated tool process.
- `shared_sandbox_container`: an internal tool was sampled through the shared
  OpenClaw Docker sandbox container cgroup, not a dedicated tool process.
- `monitor_window_no_overlap`: a PID/cgroup existed, but the sampler did not
  capture an overlapping resource window.

For complete cgroup sampling in SWE-Rebench, the task container must be able
to create per-execution cgroups under `/sys/fs/cgroup/claw`. The default
`swe_rebench/config.yaml` enables the required privileged Docker mode,
host cgroup namespace, and read-write cgroupfs mount. If those permissions are
removed, `CLAW_CGROUP_REQUIRED=1` makes launcher startup fail instead of
silently recording the container root cgroup as if it were per-tool data.

The JSON Schema contracts remain the source of truth for protocol details.
