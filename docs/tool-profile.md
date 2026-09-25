# Trace JSONL and Tool Profile Field Reference

A tool profile consists of the prediction made when a call starts, the measurements taken when the call ends, and the associated execution and subcommand telemetry. The public structure is defined by the [JSON Schema](../contracts/); the following explains how to read the values and judge whether they are usable. The different collectors work independently, and a single overall coverage figure cannot substitute for the validity of every metric.

`null` or a missing field means that measurement does not exist; `0` only means the count is zero within the current measurement scope. `available`, `eligible_for_kb`, and `memory_eligible` do not indicate prediction accuracy or resource limits. Timeouts and cancellations are incomplete executions and cannot serve as training labels for complete executions. Historical traces do not become accurate automatically just because the collector is upgraded; when validating a new collection, use a new run-local KB so that old labels do not keep influencing predictions.

This document covers all stable fields defined by ClawTune in trace v6 JSONL. For `input.requested_args`, `input.messages[]`, `output.result`, `output.content`, `provider_metadata`, `request_options`, `raw_*`, `placement`, `profiling`, and fields marked as open diagnostic objects, the internal structure is determined by the tool, model provider, OpenClaw, or collector version; this document explains the meaning of the container and does not mislist external dynamic keys as part of the ClawTune stable protocol. Each line is an independent JSON object, and its structure is determined by `record_type`.

## Quick Guide: Measurements and Predictions

Read one tool call in three parts. `span_start.prediction` is the forecast made before execution. The matching `span_end.resources` contains call-level observations. An `execution_telemetry` event joined by `execution_id` contains executable-clause evidence. A value in the prediction is not an observation, and a clause observation must not be counted again as another call.

| Question | Observation to inspect | Prediction target | Practical meaning |
| --- | --- | --- | --- |
| How long did it run? | Tool duration on `span_end`; clause `latency_ms` for an executable stage | `duration_ms` | The prediction target is retained-workload elapsed time, not necessarily the raw span duration. ToolKB uses a direct call-level label; TrieKB and LatticeKB reconstruct it from retained executable clauses. |
| How much CPU work accumulated? | `resource_observation.metrics.cpu_time.value_seconds`; clause `cpu_time_seconds` | `cpu_time_seconds` | Core-seconds, not CPU percentage. ToolKB uses the eligible owned call-level label; TrieKB and LatticeKB add retained-stage values. |
| What was average CPU use? | `resource_observation.metrics.cpu_time.average_cores` | `cpu_avg_cores` | CPU time divided by the matching duration. Multi-core values may exceed one. |
| What was the CPU burst? | `resource_observation.metrics.cpu_peak.value_cores` | `cpu_peak_cores` | Maximum average core use over a 500 ms window. This is separate from cumulative CPU time. |
| What process memory was resident? | `resource_observation.metrics.memory_peak.value_bytes` | `sampled_peak_rss_bytes` | Sampled, de-duplicated RSS of the owned process lineage. It is not environment memory and is not an exact allocation count. |
| What was the environment's highest charge? | `memory_total_peak_bytes` | `memory_total_peak_bytes` | Peak sampled cgroup or guest-environment usage, including pre-existing processes and charged cache. |
| How much did environment memory rise during this tool? | `memory_extra_peak_bytes` together with `memory_baseline_bytes` | `memory_extra_peak_bytes` | `max(0, total_peak - baseline)`. This is the closest prediction to tool-caused extra memory, but it is an environment delta rather than exclusive attribution to the tool. |
| What storage or network traffic was observed? | `metrics.disk_io`, `metrics.network_io`, and compatibility delta fields | No target in the seven-target load prediction | These remain measured diagnostics and training inputs only where a separate contract explicitly says so. |
| What hardware events occurred? | `resources.pmu` | `pmu_prediction` | Separate ToolKB hardware prediction; do not mix it with the seven load targets. |

For extra memory, always read `memory_baseline_bytes`, `memory_total_peak_bytes`, `memory_extra_peak_bytes`, `memory_measurement`, `memory_environment_id`, and `memory_eligible` together. Total and extra samples from different `memory_measurement` values are different metrics. Prediction evidence is namespaced by the same value, so an unavailable prediction can mean that history exists only under another measurement source.

Use the status fields in this order: an observation is a training label only when that metric is `eligible`; a prediction exists only when its target is `available`; `evidence_counts` reports the historical support; and `calibration=unvalidated` means availability does not establish accuracy. `unavailable` is therefore a valid outcome, while a non-null p90 remains an empirical estimate rather than a resource limit.

## Sampling Frequency and Measurement Scope

The `occurred_at` of a completion event must be a valid timestamp with a time zone; invalid input returns 422 and is not replaced with the current time. `duration_ms=0` may indicate a missing value or insufficient millisecond precision and is not used as a zero-duration training sample; independently valid PMU labels are not subject to this restriction.

| Data source | Frequency or trigger | Interpretation |
| --- | --- | --- |
| Authoritative call-level eBPF observation | perf callback roughly every 10 ms of CPU time, plus exec / exit boundary events | Not wall-clock 100 Hz; each metric's `eligible` status is judged from its own evidence. |
| cgroup-v2 fallback | takes start and end snapshots of the dedicated cgroup during the same period as eBPF, returned only when eBPF fails | Describes only the fallback collector's own window; even when CPU / I/O have values, they are not action training labels. |
| Compatibility cgroup / process-tree sampling | default polling wait of 50 ms, with separate snapshots at start and end | Used for legacy fields and historical traces; 20 Hz is the target, and the actual interval is determined by the difference between adjacent `ts` values in the timeline. |
| Environment memory `memory.current` | independent thread with a default polling wait of 50 ms | Does not share a sampling thread with process-tree enumeration or network BCC initialization; still subject to system scheduling and file-read latency. |
| Subcommand eBPF CPU / RSS | perf callback roughly every 10 ms of CPU time; plus exec / exit boundary events | Not wall-clock 100 Hz; sleeping, I/O waits, and multi-core execution change the wall-clock sampling density. |
| Subcommand disk accounting | Linux task I/O cumulative accounting from perf samples and process boundaries | Byte counts are not read/write system-call counts, and they do not include all cache-hit read traffic. |
| PMU | counters are enabled at execution and read at the end | Continuous hardware counting, not 50 ms sampling. |
| Network | TCP kernel event counting; when unavailable, may fall back to namespace cumulative counting | Does not represent complete traffic for all protocols; the subcommand network fields are currently not implemented. |
| Call and subcommand latency | difference of lifecycle events | Periodic sampling is not required. |

The authoritative call-level CPU peak uses a fixed 500 ms window; it is trainable only when attribution is exclusive, the action window can be aligned, each task's cumulative CPU boundaries are complete, and sample intervals qualify. The compatibility polling path additionally requires at least three valid samples, a single source, monotonic cumulative CPU, adjacent sample intervals of no more than 150 ms, and an untruncated timeline. A trailing portion shorter than 500 ms does not form a complete window. The subcommand CPU peak is unavailable for executions shorter than 1 second; cumulative CPU time and latency remain available. Short calls are not all discarded; only targets with insufficient evidence are unavailable.

Memory label requirements: a confirmed task container or exclusive execution cgroup, no overlapping execution windows in the same environment, a baseline no more than 150 ms before the call starts, at least one in-call sample, and sampling gaps, including the start and end boundaries, of no more than 150 ms. Hook or finalizer lifetimes may overlap when verified execution windows do not. Samples after the end do not contribute to the peak. Totals include background processes and caches; the delta represents the growth of environment usage relative to the baseline and is not guaranteed to be caused entirely by the current tool.

## JSONL Record Types

### `trace_metadata`

When the file is empty, the writer writes the metadata before the first business record; it is not written redundantly when appending to an existing non-empty file.

| Field | Meaning |
| --- | --- |
| `schema_version` | Current trace record protocol version; v6 files use the number `6`. |
| `record_type` | Fixed to `trace_metadata`. |
| `trace_format_version` | Trace format version of the entire JSONL file; currently `6`. |
| `scaffold` | The agent framework that produced the trace, for example `openclaw`. |
| `mode` | Fixed to `collect`, indicating an observation record. |
| `created_at` | UTC ISO 8601 time when the metadata was written. |
| `clock_source` | Optional; the writer's textual description of the wall-clock and monotonic clock sources. |
| `clock_precision` | Optional; clock precision description, for example best-effort nanosecond. |

### Fields Common to `span_start` and `span_end`

| Field | Meaning |
| --- | --- |
| `schema_version` | The number `6`; do not confuse it with `clawtune.v1` in nested API payloads. |
| `record_type` | `span_start` or `span_end`. |
| `gateway_id`, `runtime_id`, `run_id`, `session_id`, `agent_id`, `repo` | Gateway, runtime instance, run, session, agent, and project identity. Correlation preserves owner information and never merges across runs based only on tool name. |
| `trace_id`, `span_id`, `parent_span_id`, `sequence_no` | Trace identity, call identity, parent call, and sequence number; the start / end of the same call share the span identity. |
| `kind`, `name` | `tool` / `llm`, plus the tool name / model name. The profile tables in this document correspond to `kind=tool`. |
| `wall_time_ns`, `monotonic_time_ns` | Decimal strings; the former is Unix epoch ns, and the latter is the writer process's monotonic clock in ns. Values from different processes or different clock domains cannot be subtracted directly. For spans reconstructed by the sidecar, the monotonic start may be obtained by subtracting the duration from the end point. |

Only records produced by the sidecar's main writer carry `gateway_id`, `runtime_id`, and `repo`; equivalent records from the plugin's local writer do not carry these three fields. They are not implicit fields reconstructed from the file name.

### Fields Exclusive to `span_start`

| Field | Meaning |
| --- | --- |
| `input.requested_args` | Tool call arguments; `null` for LLM spans. When redaction is enabled, sensitive keys and values are replaced with redaction markers. The inner keys are defined by the tool. |
| `input.messages` | Optional; a snapshot of the LLM input messages. Message objects are defined by the model interface and may be redacted, truncated, or `null` depending on configuration. |
| `input.request_options` | Optional from the sidecar proxy; the options in the original model request other than `messages`. The inner keys are defined by the provider API. |
| `prediction` | The optional load / PMU / compatibility prediction saved by the sidecar before the tool executes; see the "Prediction" section for details. The plugin's local writer usually does not have this field, because its start is written to disk before the decision. |
| `execution.mode` | `launcher`, `marker`, `in_process_or_runtime_managed`, or `null`. It can be `null` when the execution mode is not yet determined at start time. |
| `execution.execution_id` | Optional ClawTune execution identity; `null` when no independent execution has been registered. |
| `model_tool_call_observation.tool_call_id` | Reserved model-side tool-call identity diagnostic. |
| `model_tool_call_observation.raw_arguments` | Reserved raw model tool arguments string. |
| `model_tool_call_observation.parse_status` | `verified` or `damaged_or_unverified`; indicates whether the model-side arguments can be reliably parsed. |
| `correlation.status`, `correlation.reason` | Whether the start/end correlation is `resolved`; when it cannot be correlated, it is `unresolved` with a reason, for example a missing tool-call ID. |

`model_tool_call_observation` is usually not produced by the current writer and is kept in the v6 types for compatibility diagnostics; a missing field does not mean the tool call failed.

### Fields Exclusive to `span_end`

| Field | Meaning |
| --- | --- |
| `duration_ns` | Duration in ns computed from the span's monotonic clock, as a decimal string. |
| `duration_sec` | Optional string representation of `duration_ns / 1e9`; the sidecar writer currently does not write this compatibility field. |
| `observed_duration_ms` | Optional; the millisecond duration reported by the OpenClaw hook, used to cross-check the monotonic-clock duration. |
| `status.code` | `ok`, `error`, `timeout`, `cancelled`, `interrupted`, or `unknown`. |
| `status.message` | Error type, non-zero exit code summary, interruption cause, or `null`. It cannot replace the raw output. |
| `output.exit_code` | The parsed exit code of the tool; built-in non-process tools may use a synthetic success code, and a missing or `null` value cannot by itself imply success. |
| `output.result` | Optional snapshot of the tool's raw result; the structure is defined by the tool and may be redacted or `null` depending on configuration. |
| `output.content` | LLM response content or tool-call content; the structure is defined by the model interface. |
| `output.provider_metadata` | Model response metadata saved by the sidecar proxy; the body and tool calls of the first choice are removed, and the remaining inner keys are defined by the provider. |
| `output.proxy` | Result of the sidecar proxy request; common keys are `status_code`, `stream`, and `error`. |
| `execution.mode`, `execution.execution_id` | Final execution mode and execution identity. |
| `execution.requested_command` | The command requested by the user, without launcher wrapping. |
| `execution.effective_command` | The command actually handed to OpenClaw for execution; it may include launcher wrapping and may be redacted. |
| `execution.payload_command` | The payload command ultimately launched by the launcher. |
| `execution.payload_pid`, `execution.payload_pid_start_time_ticks` | The payload root PID and the `/proc/<pid>/stat` starttime tick, used to prevent misattribution from PID reuse. |
| `execution.cgroup_path`, `execution.cgroup_id` | The execution cgroup path and optional cgroup identity; the presence of a path does not mean every metric comes from the cgroup. |
| `execution.pid_role` | PID role: `payload_root`, `launcher`, `unknown`, or `null`. |
| `execution.source` | Optional from the sidecar; the discovery source of the final resource scope. |
| `execution.tool_resource` | Compact summary of execution eBPF telemetry; see "Subcommand Profile and Collection Quality" for details. |
| `resources` | Call-level resources, environment memory, and PMU records; see the following sections for details. LLM spans use `not_applicable` / `none` empty resource objects. |
| `correlation.status`, `correlation.reason` | When the end cannot find the corresponding start, it is `unresolved`; a common reason is `span_start_not_found`. |

### `trace_event`

The outer protocol for supplementary events is defined in the [trace event schema](../contracts/trace-event.schema.json):

| Field | Meaning |
| --- | --- |
| `schema_version` | The number `6`. |
| `record_type` | Fixed to `trace_event`. |
| `event_type` | `execution_telemetry`, `incomplete_span`, `llm_proxy_unmatched`, or `runtime_finalization`. |
| `gateway_id`, `runtime_id` | The gateway and runtime instance to which the event belongs; they may be `null` when the owner cannot be uniquely recovered. |
| `execution_id` | The execution identity for `execution_telemetry`. |
| `kind` | `tool` or `llm` for `incomplete_span`. |
| `reason` | The reason an incomplete / unmatched event was produced, for example runtime termination or timeout. |
| `prediction` | When the `incomplete_span` is a tool, the optional prediction that has been produced but has not yet entered a complete span. |
| `payload` | Stable payload that varies with `event_type`; see the tables below. |
| `artifact` | Optional for `execution_telemetry`; the complete object after the writer inlines the clause telemetry JSON pointed to by `artifact_path`. See "Subcommand Profile and Collection Quality" for the fields. |

`payload` for `event_type=execution_telemetry`:

| Field | Meaning |
| --- | --- |
| `execution_id`, `tool_call_id` | Execution and corresponding OpenClaw tool call identity. |
| `artifact_path` | Path of the original eBPF artifact; the path may be inaccessible after the trace is copied. For inlined content, see the outer `artifact`. |
| `started` | Whether execution telemetry collection started successfully. |
| `status` | Aggregate status of execution telemetry. |
| `unavailable_reason` | Reason it did not start or is unavailable. |
| `kb_observations_added` | Number of clause observations written to the KB this time; not equal to the number of qualifying samples for all metrics. |
| `kb_update_error` | Reason the KB update failed; empty does not mean every metric is trainable. |
| `call_telemetry` | Compact call telemetry; see below for the fields. |
| `artifact_summary` | Compact summary of the artifact; see below for the fields. |

`call_telemetry` contains `tool_call_id`, `command`, `telemetry_quality`, `formal_completeness`, `eligible_for_kb`, `clause_count`, `call_resource`, and `clauses[]`. `call_resource` holds the call-level aligned `peak_cpu_cores` and `sampled_peak_rss_mb` plus per-target availability; it sums simultaneous owned exec-image contributions before taking each peak. `artifact_summary` contains `schema`, `schema_version`, `mode`, `replay_execution`, `collector`, `container_id`, `telemetry_quality`, `formal_completeness`, `telemetry_loss_total`, `call_count`. These summaries and the outer inlined `artifact` are views of the same source and must not be counted twice.

`event_type=incomplete_span` means there is only a started event and no normal completion. Its `payload` is the raw sidecar API event; the common fields are as follows:

| Field | Meaning |
| --- | --- |
| `schema_version` | Nested API protocol identifier `clawtune.v1`. |
| `event_id`, `occurred_at`, `plugin_version` | Event identity, time-zone-aware ISO 8601 time, and plugin version. |
| `gateway_id`, `runtime_id`, `repo` | Owner and project identity. |
| `run_id`, `session_id`, `session_key`, `agent_id` | OpenClaw run, session, raw session key, and agent identity. |

When `kind=tool`, the payload additionally contains:

| Field | Meaning |
| --- | --- |
| `tool_call_id`, `tool_name`, `tool_kind`, `tool_input_kind` | Tool call identity, name, and the tool / input category provided by OpenClaw. |
| `operation_hint` | Optional operation hint extracted from the request. |
| `derived_paths` | List of paths extracted from the arguments; used for features and diagnostics and does not mean all were accessed. |
| `params_digest` | A stable digest of the arguments, not reversible argument content. |
| `param_features.serialized_size_bytes` | Serialized size of the arguments in bytes. |
| `param_features.string_length` | Aggregate length feature of string content in the arguments. |
| `param_features.list_item_count` | Feature for the number of list items in the arguments. |
| `param_features.path_count` | Feature for the number of detected paths. |
| `param_features.has_command_like_field` | Whether it contains a command-like field. |
| `raw_params`, `raw_event` | Optional raw arguments and hook event; inner keys are defined by the tool / OpenClaw and may be redacted. |
| `resource_scope` | The PID / cgroup scope known at request time; see the table below for the fields. |

When `kind=llm`, the payload additionally contains `event_type`, `call_id`, `provider`, `model`, `duration_ms`, `outcome`, `context_token_budget`, `raw_input`, `raw_output`, `raw_event`, which respectively represent the started / ended type, call identity, provider, model, known duration, outcome, context token budget, and the open raw input, output, and hook event.

Stable fields of `resource_scope`:

| Field | Meaning |
| --- | --- |
| `kind` | `pid` or `cgroup-v2`. |
| `execution_id` | The execution corresponding to the scope. |
| `pid`, `root_pid` | Discovered PID and trusted execution root PID. |
| `process_start_time`, `root_starttime_ticks` | Process start identity; the latter is the Linux starttime tick. |
| `cgroup_path`, `pid_namespace_inode`, `container_id` | Cgroup path, PID namespace inode, and container identity. |
| `include_children` | Whether collection should include descendant processes. |
| `source`, `attribution_source` | How the scope was discovered and the basis for exclusive / shared attribution. |

`event_type=llm_proxy_unmatched` means a proxy request was not matched to an OpenClaw model completion. The payload fields are as follows:

| Field | Meaning |
| --- | --- |
| `type`, `action_type` | Fixed to `action`, `llm_call`. |
| `action_id`, `run_id`, `session_id`, `session_key`, `agent_id`, `runtime_id` | Proxy action and available owner identity; missing values are `null`. |
| `ts_start`, `ts_end` | Unix-second start and end of the proxy request. |
| `data.provider`, `data.model` | Model provider and model name. |
| `data.messages_in`, `data.content` | Input messages and output content; if `raw_request.messages` or `raw_response.choices` is saved, the corresponding duplicate fields are omitted. |
| `data.duration_ms`, `data.llm_latency_ms` | Integer-ms summary and floating-point ms value of the same proxy wall-clock duration. |
| `data.outcome`, `data.context_token_budget` | `completed` / `error` outcome and optional context budget. |
| `data.proxy.status_code`, `.stream`, `.error` | HTTP status, whether streaming, and error text. |
| `data.openclaw_started_event`, `data.openclaw_ended_event` | Corresponding OpenClaw hook event; usually `null` for unmatched records. |
| `data.raw_request`, `data.raw_response` | Open raw objects of the provider protocol. |

`event_type=runtime_finalization` means the runtime has completed unified termination and cleanup:

| payload field | Meaning |
| --- | --- |
| `finalized` | Whether finalization completed; currently `true` for successful records. |
| `gateway_id`, `runtime_id`, `reason` | The terminated owner and the reason: task timeout (historical traces may still carry `agent_timeout`), cancellation, or runtime stop. |
| `aborted_execution_ids` | List of executions aborted according to the termination reason without complete exit evidence. |
| `pmu_profiles` | PMU profiles at termination, indexed by execution ID; see the PMU section for the fields. |
| `observation_errors` | List of errors during finalization; each item contains `execution_id` and `error`. |

The `trace_flushed` in the HTTP abort response is a persistence confirmation returned to the caller; it is not written into `runtime_finalization.payload`, because that trace line cannot confirm the flush that happens to it afterward.

## Authoritative Call-Level Measurements: `resources.resource_observation`

See the [tool resource observation schema](../contracts/tool-resource-observation.schema.json). This object is the authoritative record of resource measurement source, window, attribution, and training eligibility. `available=true` only means a value exists under the current measurement definition; only `eligible=true` means the metric can serve as a training label for its exclusively attributed workload target. The two must be read per metric; an object-level coverage or another metric cannot substitute for them.

Finalized execution observations may be eligible with `reason=execution_window_only` and `window.complete=false`: CPU totals and sampled peaks describe the owned workload, excluding unmeasured shell/hook overhead. Average cores uses the same CPU total divided by the full tool duration in both training and trace replay. Compare predictions against those same labels. Pipeline predictions exclude only listed downstream consumers; standalone `head`/`cat` and the first pipeline stage remain included. The whole-tool observation can still include consumer overhead.

Environment-memory baselines may reuse a sample from the same verified environment taken at most 150 ms before the tool window. Overlapping calls, stale baselines and missing in-window samples remain ineligible; a first call without a pre-start sample can still have no memory label.

| Field | Meaning |
| --- | --- |
| `schema` | Fixed to `tool_resource_observation_v1`. |
| `backend` | The collector that actually returned the observation: `ebpf` is preferred, and `cgroup-v2` may be used on failure. |
| `fallback_used` | Whether the cgroup-v2 fallback was returned because the preferred eBPF failed. Sparse sampling by itself does not switch the backend. |
| `fallback_reason` | When the fallback is used, the reason the preferred eBPF failed. |
| `scope` | Actual measurement scope: `process_tree`, `cgroup-v2`, or `none`. |
| `attribution` | `exclusive_process_tree`, `shared_scope`, or `unattributed`. Resource values from the latter two cannot be used for training. |
| `unavailable_reason` | The reason the entire preferred observation is unavailable; each metric still has its own `reason`. A successful cgroup fallback removes this field and keeps `fallback_reason`. |
| `window` | The observation's own time range and coverage information; see the table below for the fields. |
| `metrics` | `cpu_time`, `cpu_peak`, `memory_peak`, `disk_io`, `network_io`, and the optional `memory_charge_peak`. |
| `loss` | eBPF loss counts within the current observation window; any positive value makes the preferred observation unavailable. |
| `execution_telemetry_quality` | Copies the telemetry quality when the observation comes from the final execution artifact. |

All nanosecond timestamps in `window` belong to the clock domain declared by `clock`; values from different domains cannot be subtracted or compared directly:

| Field | Meaning |
| --- | --- |
| `kind` | Optional window semantics: `action`, the fallback's own `collector`, or `execution` for final execution telemetry; older action observations may omit it. |
| `clock` | `linux_monotonic`, `openclaw_plugin_process_monotonic`, or `sidecar_synthetic_duration_anchor`. The latter two are not equal to the Linux kernel monotonic clock. |
| `requested_start_ns`, `requested_end_ns` | Requested boundaries of the measurement window. The `collector` window is the boundary of the fallback's two snapshots, not an inferred action boundary. |
| `observed_start_ns`, `observed_end_ns` | First and last times with actual collection evidence; `null` when there is no evidence. |
| `coverage_ratio` | Span of actual evidence time / requested window duration; `null` means it cannot be computed. It is not sampling density, nor does it directly determine whether any metric is trainable. |
| `max_sample_gap_ns` | The largest edge or inter-sample gap encountered by the object-level coverage check. |
| `complete` | Whether the requested window is covered under the current coverage rules; each metric may still be disqualified by its own boundaries, samples, or attribution. |
| `late_scope_binding` | The authoritative scope was bound only after collection started; existing values are kept, but no metric may be used for training. |
| `action_clock_unusable` | The action boundaries cannot be safely aligned with the collection clock; existing values are kept, but they must not be used as action labels. |

Every `metrics.*` uses the same status shell:

| Field | Meaning |
| --- | --- |
| `available` | Whether a measured value exists under the current `measurement` and window. |
| `eligible` | Whether it can serve as a training label for its exclusively attributed workload target; fallback collector-window CPU / I/O can be available but is never eligible. |
| `reason` | Metric-level reason for availability or eligibility; `ok` means eligible. Common rejection reasons include `shared_scope`, `sampling_gap`, `insufficient_samples`, `action_clock_window_unusable`, `scope_bound_after_action_start`, and `collector_window_only`. |
| `measurement` | Measurement semantics identifier; the definition must not be guessed from the field name alone. |
| `value_seconds`, `average_cores` | Cumulative core-seconds for `cpu_time`, and the average core count over the complete action window. |
| `value_cores`, `window_ms` | The fixed-window core count and window milliseconds for `cpu_peak`; the current window is 500 ms. |
| `value_bytes` | Bytes for `memory_peak` or `memory_charge_peak`; must be interpreted together with `measurement`. |
| `read_bytes`, `write_bytes` | Read / write bytes for `disk_io`, or received / sent bytes for `network_io`. |
| `sample_count` | Number of contributing samples exposed by the collector; `null` means the final execution summary does not have this count. |
| `counter_exact` | Whether `value_bytes` is an exact count; `false` for sampled RSS. |
| `window_start_ns`, `window_end_ns` | Boundaries when the metric has its own collector window. For example, the network counting window may differ from the requested window. |

The current measurement definitions are as follows:

| Metric | eBPF measurement | Value semantics |
| --- | --- | --- |
| `cpu_time` | `ebpf_task_cpu_time` or `ebpf_owned_lineage_cpu_time` | Cumulative CPU of the owned task lineage; only a complete action window provides a trainable `average_cores`. |
| `cpu_peak` | `ebpf_task_cpu_500ms_peak` or `ebpf_owned_lineage_cpu_500ms_peak` | Maximum of the 500 ms average CPU; multi-clause calls sum owned exec-image contributions in aligned windows before taking the maximum. |
| `memory_peak` | `ebpf_sampled_distinct_mm_rss` | Sampled RSS peak after de-duplicating mm across the owned lineage; it is neither environment memory nor cgroup charge. |
| `disk_io` | `ebpf_task_io_accounting` | Read/write cumulative difference of owned task I/O accounting. |
| `network_io` | `ebpf_tcp_send_recv_bytes` | TCP send/receive bytes; explicitly unavailable when there is no supporting evidence, and currently not provided by the final execution summary. |

The cgroup-v2 fallback is enabled only for exclusive, non-root execution cgroups. `cpu_time` and `disk_io` use `cgroup_v2_cpu_stat` and `cgroup_v2_io_stat` respectively and record the difference between the two snapshots, but `reason=collector_window_only` and `eligible=false`. It does not sample a CPU peak and has no network counts. `memory_charge_peak` is added only when `memory.peak` strictly increases between the two snapshots: its `value_bytes` is the absolute lifetime high-water charge, not a delta, RSS, or action label.

## Other Call-Level Fields and Legacy Fields: `span_end.resources`

When `resource_observation` is present, the current training code relies on its per-metric `eligible` and `attribution`; compatibility values are projected from the same observation into legacy fields. Only historical traces without `resource_observation` use the old coverage / sampling rules. Environment memory and PMU are independent measurements, not legacy aliases of that object.

| New field | Legacy / compatibility field | Migration meaning |
| --- | --- | --- |
| `window.*` | `monitor_*`, `coverage_*`, `action_monotonic_clock_domain` | The new object declares both the window kind and the clock domain; legacy fields alone are insufficient to prove a metric is trainable. |
| `metrics.cpu_time.value_seconds` | `cpu_time_s`, `cgroup_cpu_time_s` | `cpu_time_s` is a general compatibility projection; `cgroup_cpu_time_s` is only a legacy cgroup-source alias. |
| `metrics.cpu_time.average_cores` | `cpu_utilization_avg_cores`, `cpu_utilization_avg_pct` | The percentage is the core count multiplied by 100; eligibility follows the new metric's `eligible`. |
| `metrics.cpu_peak.value_cores` | `cpu_peak_cores` | Both currently represent the fixed 500 ms window peak; see `window_ms` / `cpu_peak_window_ms` for the window. |
| `metrics.memory_peak.value_bytes` | `rss_peak_bytes` | The new field is explicitly sampled distinct-mm RSS; the legacy field used to mix in cgroup `memory.current`, so it cannot be migrated without knowing the source. |
| `metrics.disk_io.read_bytes`, `.write_bytes` | `disk_read_bytes_delta`, `disk_write_bytes_delta` | Both are cumulative differences within the respective measurement window; eligibility depends only on the new metric. |
| `metrics.network_io.read_bytes`, `.write_bytes` | `net_rx_bytes_delta`, `net_tx_bytes_delta` | Received / sent bytes respectively; the collection scope must be interpreted together with `measurement`. |
| `metrics.memory_charge_peak` | No RSS-equivalent field | Cgroup charge is kept separately and must not be written as or interpreted as RSS. |
| `available`, `eligible`, `reason`, `measurement` | No complete equivalent fields | These are new per-metric semantics; legacy object-level quality / coverage cannot override them. |

| Field | Unit and meaning |
| --- | --- |
| `action_duration_ns`, `tool_body_ns` | ns; the action duration reported by OpenClaw; the latter may be null when there is no raw duration. It includes overhead within the framework execution boundary and is not equal to the subcommand duration. |
| `plugin_window_ns` | ns; the monotonic-clock interval between the plugin's before / after hooks. |
| `decision_duration_ns` | ns; time spent on decision / instrumentation in the before hook. |
| `completion_duration_ns` | ns; round-trip time for the after hook to report completion; when the sidecar writes the trace the round trip has not finished, so it may be null. |
| `sidecar_overhead_ns` | ns; the sidecar overhead known to the current producer. The sidecar trace usually contains only the before hook part and must not be treated as total overhead. |
| `action_monotonic_clock_domain` | Source of the action monotonic clock; only `linux_monotonic` can be directly aligned with kernel eBPF time, and when it is unusable the sidecar writes `sidecar_synthetic_duration_anchor`. |
| `monitor_start_wall_time_ns`, `monitor_end_wall_time_ns` | Unix ns; actual start and end of the compatibility sampling window. Null when the eBPF monotonic clock cannot be safely converted to wall clock. |
| `monitor_start_monotonic_ns`, `monitor_end_monotonic_ns`, `monitor_duration_ns` | ns; the monotonic-clock boundaries and duration of the authoritative observation projection or the compatibility sampler. For the exact clock domain, see `action_monotonic_clock_domain` or `resource_observation.window.clock`. |
| `coverage_duration_ns`, `coverage_ratio` | ns, 0–1; the intersection and ratio of the compatibility window and the action. When `resource_observation` exists it is projected from its coverage and cannot override the per-metric `eligible`. It is not sampling density, nor is it the coverage of independent memory or PMU. |
| `coverage_reason` | Window reason, such as `full_window`, `pid_registered_late`, `monitor_window_no_overlap`, `shared_runtime_process`, `shared_sandbox_container`, `pid_unavailable`, `clock_data_missing`. |
| `attribution_status`, `attribution_source`, `scope` | Compatibility attribution, discovery source, and scope; for new records the scope can also be `cgroup-v2`. `attributed` does not guarantee complete sampling; for the authoritative value see `resource_observation.attribution`. |
| `quality`, `sampling_quality` | Compatibility window and sampling quality summary. They cannot override `metrics.*.eligible`; `ok` in legacy polling records also does not guarantee that 50 ms was actually achieved. |
| `monitor_source`, `target_pid` | The actual resource sampler and target PID. |
| `sampling_interval_ms`, `sampling_point_count` | ms, count; the nominal compatibility interval and CPU sample count. eBPF records usually show 10 ms and fallback shows 0; neither is the actual average wall-clock interval. |
| `cpu_time_s`, `cgroup_cpu_time_s` | core-s; compatibility cumulative CPU values. For new records, `cpu_time_s` is projected from the authoritative observation; `cgroup_cpu_time_s` is a legacy cgroup-source alias and must not be added to the former. For training eligibility see `metrics.cpu_time.eligible`. |
| `cpu_utilization_avg_cores` | cores; for new records it is projected from a qualifying `metrics.cpu_time.average_cores`. For legacy records it is computed only when the monitor and action start/end each differ by no more than 1 ms. |
| `cpu_utilization_avg_pct` | %; `cpu_utilization_avg_cores * 100`; multi-core values can exceed 100%. |
| `cpu_peak_cores`, `cpu_peak_window_ms` | cores, ms; the compatibility fixed-window CPU peak and the 500 ms window. For training eligibility see `metrics.cpu_peak.eligible`. |
| `rss_peak_bytes`, `memory_rss_bytes_before`, `memory_rss_bytes_after` | bytes; historically named diagnostic values. For new eBPF records, `rss_peak_bytes` projects sampled distinct-mm RSS; older cgroup values may actually be `memory.current`. Do not mix across sources; when training on sampled RSS use `metrics.memory_peak`. |
| `memory_baseline_bytes` | bytes; the most recent qualifying pre-call environment memory sample. |
| `memory_total_peak_bytes` | bytes; maximum in-call environment memory sample, not an allocation limit. |
| `memory_extra_peak_bytes` | bytes; `max(0, total_peak - baseline)`. It is an environment high-water delta, not the tool process's exclusive allocation or RSS delta. |
| `memory_environment_id`, `memory_measurement` | Environment identity and measurement method: `cgroup_v2_memory_current` for one stable cgroup or `cgroup_v2_environment_union_v1` for the deduplicated base-plus-execution scope; the protocol also supports `guest_memtotal_minus_memavailable`, which is not host VM RSS. |
| `memory_eligible`, `memory_unavailable_reason` | Whether there is evidence for a memory label and the reason it is unavailable, independent of the process window coverage. |
| `memory_timeline` | `[Unix seconds, bytes]` samples; qualifying output includes the baseline and in-call samples. |
| `memory_diagnostics` | Present for a readable but ineligible memory window. It records the selected window, baseline sample, in-window samples, observed baseline/total/extra arithmetic, exclusivity, and truncation. These nested values are audit evidence only and never enter a KB. |
| `memory_clause_observations` | Training observations generated from the same timeline for non-overlapping subcommands; they are not additional memory consumption and must not be summed repeatedly. |
| `disk_read_bytes_delta`, `disk_write_bytes_delta` | bytes; cumulative storage I/O difference over the sampling interval. |
| `disk_read_bytes_per_s`, `disk_write_bytes_per_s` | bytes/s; the above differences divided by the action duration in seconds. |
| `net_rx_bytes_delta`, `net_tx_bytes_delta` | bytes; received / sent counter difference. There is a scope difference between per-process TCP and the namespace fallback, and a zero value cannot prove that all protocols had no traffic. |
| `net_rx_bytes_per_s`, `net_tx_bytes_per_s` | bytes/s; the above differences divided by the action duration in seconds. |
| `ctx_switches_delta` | count; cumulative context-switch difference over the sampling interval; may miss processes that have exited. |
| `process_count_before`, `process_count_after` | count; number of visible processes at start and end, not the total created during the interval. |
| `resource_class` | Category based on predicted latency, not a measured CPU / memory classification. |
| `resource_timeline`, `resource_timeline_truncated` | Resource sample list from the legacy polling path and whether it exceeded the storage cap (2000 points by default); new eBPF call observations do not copy the timeline here. When a legacy record is truncated, peak labels are unavailable. |
| `resource_observation` | Authoritative per-metric resource observation; see the previous section for the fields and precedence. |
| `cgroup_resource`, `pmu` | Legacy sampling summary and independent hardware-counter profile; see below for details. |
| `cgroup_artifact_path` | Optional relative artifact path kept in the plugin v6 types; the current writer usually does not produce it, and its absence should not be taken to imply collection failure. |

Memory unavailability reasons: `unverified_task_environment` (unverified environment such as a host service), `execution_window_unavailable` (missing window), `overlapping_environment_calls` (overlapping calls), `baseline_after_execution_start` (baseline too late), `stale_memory_baseline` (baseline too old), `no_in_execution_memory_sample` (no in-call sample), `memory_sampling_gap` (sampling gap), `memory_timeline_truncated` (the tool ran longer than the memory timeline retention range, so the complete window cannot be safely reconstructed). Memory fields may also be absent when the cgroup is missing or reading fails. Short calls with no samples are treated as unavailable. For exec, a verified Linux-monotonic execution interval selects the payload window; otherwise the action interval remains the conservative fallback.

`memory_clause_observations[]` contains `repo`, `bin`, `argv`, `ts_start`, `ts_end` (project, subcommand, and Unix-second boundaries), `in_loop`, `in_pipe`, `in_subst`, `pipeline_position` (command structure), and the identically named `memory_baseline_bytes`, `memory_total_peak_bytes`, `memory_extra_peak_bytes`, `memory_measurement`, `memory_environment_id`, `memory_eligible`. To reuse the observation structure, it also keeps `latency_ms`, `cpu_peak_cores`, `sampled_peak_rss_mb`, `cpu_ns_cumulative`; in this memory-only observation they are null and will not cause CPU and latency to be trained twice.

Each point in `resource_timeline[]` contains `ts` (Unix seconds), `elapsed_ms` (milliseconds since the first point of that segment), `available`, `source`, `rss_bytes`, `process_count`. The cumulative difference fields are `cpu_time_delta_s`, `read_bytes_delta`, `write_bytes_delta`, `net_rx_bytes_delta`, `net_tx_bytes_delta`, `ctx_switches_delta`, relative to the first point of the same-source segment. `read_bytes_per_s`, `write_bytes_per_s`, `net_rx_bytes_per_s`, `net_tx_bytes_per_s` use the adjacent-sample interval as the denominator, unlike the call-level rate denominator. The rate for the first point of a segment is undefined.

`cgroup_resource` is another compatibility representation of legacy cgroup / process-tree runtime sampling results, not an additive measurement; the new eBPF / dedicated fallback path uses `resource_observation` as authoritative and usually does not produce this object:

| Field | Meaning |
| --- | --- |
| `schema`, `execution_id`, `tool_call_id`, `tool_name` | `cgroup_resource_v1` and the associated identities. |
| `source`, `monitor_source`, `attribution_source` | Whether it is actually a cgroup or a process tree, and the source. The object name does not guarantee that a cgroup is used. |
| `ts_start`, `ts_end`, `duration_ms` | Unix-second start and end and millisecond duration of the action. |
| `cpu_time_s`, `cpu_utilization_avg_cores` | Same as the identically named fields in the previous table. |
| `memory_rss_before_bytes`, `memory_rss_after_bytes`, `memory_rss_peak_bytes` | Correspond to the RSS diagnostic fields in the previous table and likewise must be interpreted according to source. |
| `disk_read_bytes_delta`, `disk_write_bytes_delta` | Storage read/write byte differences. |
| `network_rx_bytes_delta`, `network_tx_bytes_delta` | Correspond to the top-level `net_rx_bytes_delta`, `net_tx_bytes_delta`. |
| `sampling_interval_ms`, `sampling_point_count`, `sampling_quality`, `sampling_coverage_ms` | Target interval, point count, quality, and monitoring duration; the last is not the action intersection duration. |
| `cpu_source`, `memory_source`, `disk_source`, `network_source` | Source descriptions for each target; the network source label currently cannot distinguish BCC from namespace fallback. |
| `fallback_used`, `cgroup_setup_error`, `cgroup_read_error`, `collector_errors`, `independence` | Fallback flag, errors, and collection-relationship description. An empty error list does not prove that nothing was missed during collection; `independence` means independent of subcommand collection. |

## Subcommand Profile and Collection Quality

The entry points are `execution.tool_resource.call_telemetry` or `trace_event.artifact.calls[]`; use `call_resource` for aligned call peaks and `clauses[]` for executable-stage observations. See the [clause telemetry schema](../contracts/clause-telemetry.schema.json). The two may represent the same execution and must not be counted twice.

| Field | Unit and meaning |
| --- | --- |
| `bin`, `argv` | Subcommand executable and arguments. |
| `ts_start`, `ts_end`, `latency_ms` | Unix-second start and end and millisecond latency; recorded from execution boundaries, and periodic sampling is not required. |
| `cpu_ns_cumulative`, `cumulative_cpu_s` | Cumulative CPU in ns and the core-s value after `/1e9`; the latter is a human-readable form of the same number. |
| `cpu_time_seconds` | Cumulative CPU seconds extracted by the execution summary from clause provenance; same source as the above fields but may be null when provenance is missing. |
| `t_exec_ns`, `t_end_ns` | Linux monotonic exec / end boundaries kept by the execution summary, used to determine whether the whole action is covered. |
| `peak_cpu_cores` | cores; subcommand fixed-window CPU peak; empty when the execution is shorter than 1 second or the quality is insufficient. |
| `sampled_peak_rss_mb`, `peak_memory_mb` | Decimal MB (1 MB = 1,000,000 bytes); the former is the RSS diagnostic field of the raw artifact and the latter is the corresponding field in the summary; neither is an environment memory prediction target. |
| `disk_read_bytes`, `disk_write_bytes` | Storage I/O bytes in the summary. |
| `disk_io.read_bytes_total`, `disk_io.write_bytes_total`, `disk_io.read_write_bytes_total` | Cumulative reads, writes, and their sum from the raw artifact, in bytes. |
| `disk_io.cancelled_write_bytes_total`, `disk_io.availability` | Count of cancelled writes and availability; the cancelled-write count is kept separately and is not added in again. |
| `network_rx_bytes`, `network_tx_bytes` | Reserved subcommand network bytes; currently unavailable. |
| `status.state`, `status.exit_code`, `status.signal`, `status.succeeded` | Execution state, exit code, terminating signal, and whether it succeeded; the state can be exited, signaled, not_executed, exec_failed, or unavailable. |
| `status.reason`, `status.source` | The reason the state is missing or terminated, and the source of the state evidence. |
| `availability.latency`, `availability.cpu_time`, `availability.cpu`, `availability.memory`, `availability.disk_io`, `availability.status` | Availability and reason for each target; `cpu_time` is cumulative CPU and `cpu` is the fixed-window peak, and the two are independent. |
| `in_loop`, `in_pipe`, `in_subst`, `pipeline_position` | Whether it is in a loop, pipe, or substitution expression, and the pipeline position. |
| `eligible_for_kb`, `telemetry_quality` | Whether it can be used for learning and the collection quality; downstream pipeline viewers etc. also apply independent semantic filtering. |
| `mapping_evidence`, `owned_exec_image_count`, `provenance` | Evidence mapping static commands to running processes, the number of owned exec images, and lower-level collection diagnostics. |

`execution.tool_resource` summary fields: `started` (whether collection started), `status` / `unavailable_reason` (telemetry status), `execution_id` / `tool_call_id` (associated identities), `artifact_path` (original path, which may be inaccessible after download), `artifact_summary` (collector summary), `call_telemetry` (command, quality, subcommands), `kb_observations_added` (number of observations written), `kb_update_error` (learning update error). `call_telemetry.clause_count` is the clause count before compacting, and `formal_completeness` indicates complete / partial / unavailable. The written count is not the number of valid samples for all targets.

The raw artifact's `schema`, `version`, `mode`, `status_model` define the format; `container_id`, `cgroup_id`, `quota_cores` define the environment and CPU quota. `telemetry_quality`, `collection_validity`, `formal_completeness`, `replay_execution`, `integrity` respectively describe quality, collection validity, completeness, execution completion state, and consistency errors. `collector` contains status, health, disable reason, valid / invalid call counts, and `kprobe_total_hits`; the hit count is not a tool resource count. `cleanup` is the cleanup result, and `provenance` is the collection parameters and evidence.

In `telemetry_loss_total`, `ringbuf_reserve_failures`, `argv_read_failures`, `argv_boundary_read_failures`, `total` are loss / read failure counts; `ring_loss_total` is the ring-loss diagnostic. In `call_coverage`, `eligible_call_count`, `withheld_call_count`, `total_call_count`, `eligible_fraction` are the learnable, withheld, and total call counts and the fraction, not periodic sampling coverage.

Each artifact call additionally has `command`, `tool_call_id`, `version`, `tool_trace_ref` (input and correlation), `telemetry_quality`, `eligible_for_kb`, `target_availability`, `invalid_reasons`, `integrity`, `telemetry_loss`, `ring_loss` (quality), `mapping`, `candidate_rejections`, `coverage_gaps`, `no_runtime_exec`, `runtime_invocations`, `static_word_intent`, `transition_graph`, `provenance` (command mapping, rejected candidates, coverage gaps, unexecuted branches, and execution attribution evidence). These open diagnostic objects must not be used as stable resource metrics; in particular, unexecuted branches must not be counted as zero-duration training samples.

## PMU: `resources.pmu`

See the [PMU profile schema](../contracts/pmu-profile.schema.json). Four `events` counters and five `derived` metrics make up nine hardware profile targets:

| Field | Unit / formula |
| --- | --- |
| `events.cycles` | cycles; CPU cycles. |
| `events.instructions` | instructions; retired instructions. |
| `events.llc_read_accesses` | count; last-level cache read accesses. |
| `events.llc_read_misses` | count; last-level cache read misses. |
| `derived.ipc` | instructions/cycle; instructions / cycles. |
| `derived.llc_mpki` | misses / 1000 instructions; 1000 × misses / instructions. |
| `derived.llc_miss_rate` | 0–1; misses / accesses. |
| `derived.llc_read_accesses_per_cpu_second` | count / counter running second; accesses / `(time_running_ns/1e9)` of the corresponding event. |
| `derived.llc_read_misses_per_cpu_second` | count / counter running second; misses / `(time_running_ns/1e9)` of the corresponding event. |

Each event contains `supported`, `semantics`, `raw_count`, `scaled_count`, `time_enabled_ns`, `time_running_ns`, `running_ratio`, `error`, respectively indicating supportability, hardware semantics, raw count, scaled count, enabled time, running time, running / enabled ratio, and error. Ratios with a zero denominator are unavailable. These metrics are not DRAM bandwidth. When a simulator executes, the counts include the simulation work performed by the host and cannot be interpreted as a native guest instruction profile.

The remaining PMU fields are `schema`, `execution_id`, `source`, `mode`, `scope`, `root_pid`, `started_at`, `ended_at`, `architecture`, `pmu_devices`, `llc_semantics`, `llc_semantics_confirmed`, `collector_errors`, describing the version, identity, collection source / mode / scope, root process, Unix-second start and end, architecture, devices, whether the LLC semantics are confirmed, and errors. `coverage.status`, `reason`, `running_ratio`, `multiplexed`, `kernel_included`, `root_and_future_descendants`, `eligible_for_kb` describe reliability, reason, running ratio, multiplexing, whether the kernel and descendants are included, and whether it can be used for learning. Process sampling coverage cannot substitute for this determination.

## Prediction: `span_start.prediction`

The independent tool-level load predictions are `prediction.tool` (ToolKB), `prediction.trie` (TrieKB), and `prediction.lattice` (LatticeKB), each using the [call-load contract](../contracts/call-load.schema.json). They never fill missing targets or clauses from another model. `call_prediction` is a compatibility alias of `tool`; backend metadata retains the historical name `runtime` for ToolKB. The hardware prediction is [pmu_prediction](../contracts/pmu-prediction.schema.json). `duration_p50_ms`, `duration_p90_ms`, `resource_class`, `confidence` are top-level compatibility summaries; `confidence` is not a calibrated probability of success.

Each of `tool.targets`, `trie.targets`, and `lattice.targets` contains all seven targets:

| Target | Unit | Meaning |
| --- | --- | --- |
| `duration_ms` | ms | Retained-workload elapsed time. ToolKB uses the direct call-level retained-workload label; TrieKB and LatticeKB reconstruct it from clause evidence. It may differ from raw span duration. |
| `cpu_time_seconds` | core_seconds | Cumulative CPU of the attributed workload. |
| `cpu_avg_cores` | cores | Cumulative CPU / duration of the same observation. |
| `cpu_peak_cores` | cores | Fixed 500 ms window peak. |
| `sampled_peak_rss_bytes` | bytes | eBPF sampled distinct-mm RSS peak; separate from environment memory charge. |
| `memory_total_peak_bytes` | bytes | Environment memory sampling peak, including background. |
| `memory_extra_peak_bytes` | bytes | Non-negative delta of the environment peak relative to the baseline. |

Each target has `status`, `unit`, `metric_definition`, `avg`, `p50`, `p90`, `buckets`, `backend`, `method`, `evidence_counts`, `sample_count`, `context`, `assumptions`, `calibration`, `unavailable_reason`: availability, unit, measurement definition, mean, median, empirical p90, histogram, backend, direct / synthetic method, historical evidence count, statistical sample count, matching context, assumptions, calibration status, and unavailability reason. Statistical values of an unavailable target must be empty. p50 is the median, and p90 is the `ceil(0.9n)`-th value after sorting; it is not a guarantee that 90% of future values will fall below it. For synthetic predictions, `sample_count` can be 2048 simulation runs, while the historical sample count is in `evidence_counts`.

`buckets.edges`, `interval`, `probabilities` are the boundaries, the left-closed right-open rule, and the probability of each bucket; there is one bucket from 0 to the first boundary, one between adjacent boundaries, and one from the last boundary to positive infinity. The probabilities sum to 1.

`tool.schema_version` (likewise `trie` and `lattice`), `scope`, `lifecycle`, `cpu_peak_window_ms`, `quantile_method`, `memory_measurement` declare the protocol, call scope, lifecycle, peak window, quantile method, and memory source. Each item in `clause_predictions[]` has `clause_index`, `argv`, `cwd`, `env_names`, `scope`, `targets`, `memory_measurement`, representing the subcommand index, arguments, working directory, environment variable names, and the same seven-target prediction; subcommand duration and call duration are not interchangeable.

`pmu_prediction.targets` contains the nine PMU targets above. Each item has `status`, `unit`, `metric_definition`, `avg`, `p50`, `p90`, `backend`, `method`, `evidence_count`, `context`, `calibration`, `unavailable_reason`. Note that this is the singular `evidence_count` and has no `sample_count` or `buckets` from the load prediction. Its `schema_version`, `scope`, `lifecycle`, `quantile_method` declare the protocol, scope, complete-execution-profile lifecycle, and quantile method.

ToolKB learns eligible call-level observations directly; its retained-workload duration comes from the measured retained-clause interval when that execution evidence is required. TrieKB and LatticeKB independently select evidence for each retained clause and then use the same composer; none of the four KBs fills another model's missing evidence. For multiple clauses, duration sums serial-group maxima, cumulative CPU sums every retained stage, and CPU/RSS peaks take the maximum across serial groups after conservatively summing stages in each pipeline group. The composer draws 2048 synthetic combinations from independent clause marginals; these draws are not additional historical evidence and do not preserve cross-clause correlation.

`prediction.edge_kappa` uses the same `CallLoadPrediction` structure. It predicts duration from weighted historical time observations, with call-level and `clause_predictions[]` results containing `avg`, `p50`, `p90` and bucket probabilities. CPU and memory targets return `unavailable` with `edge_kappa_time_only`. A single clause uses an exact weighted mean, a midpoint median when cumulative weight equals 50%, and an inverse-CDF P90 (`weighted_midpoint_p50_inverse_cdf_p90`); multiple clauses use the shared 2048-draw composer. Its displayed histogram describes this empirical duration distribution. The small uniform bucket smoothing used to train edge weights is excluded because it has no observed duration. Evidence containing a censored or missing exact duration returns `non_exact_duration_evidence`; bucket midpoints are never substituted.

EdgeKappaKB learns from admitted clause telemetry after tool completion and persists in `edge-kappa-kb.json`. Writable three-KB states initialize it from their committed raw clause history; newly generated seeds include it. Frozen legacy seeds without this snapshot leave the fourth result unavailable rather than training during evaluation.

Parallel benchmark batches merge EdgeKappa feedback in task-ID order, then completion-time/event-ID order within each task. The saved execution-time predictions supply gradients applied to the current shared weights; duplicate feedback is ignored. Tasks may discover related commands in different orders: the merged KB retains their historical parent edges and applies each saved gradient to its original edges. Legacy task snapshots with learned weights but no replayable feedback must be rerun. A frozen seed that declares an EdgeKappa snapshot must supply that file and its matching hash.

Memory evidence is also separated by `memory_measurement`. The current prediction request path uses the default `cgroup_v2_environment_union_v1` namespace. Native non-shell observations can instead be stored under `cgroup_v2_memory_current`; until request routing selects that namespace, ToolKB total/extra memory can be unavailable even though eligible observations exist under the other source. Interpret this as an incompatible-source query, not as zero memory use or proof that collection failed.

TrieKB and LatticeKB tool predictions are estimates with narrower scope than a complete tool observation. Their duration and average CPU assume zero hook overhead. Multi-clause average CPU sums sampled clause CPU time and divides by the sampled composed duration; serial durations sum and pipeline durations take their maximum. Multi-clause environment total and extra memory take the maximum sampled clause prediction. The environment approximation does not reconstruct a shared baseline or joint timeline. Listed downstream pipeline consumers are excluded from both KBs for every clause target and omitted from composition; the same executable remains eligible when it runs alone or in the first pipeline position. An available result therefore describes only the retained workload. Individual retained-clause predictions remain available when the full composition cannot be formed. Short or partial observations do not automatically become eligible training labels.

`diagnostics.backends.runtime`, `.trie`, `.lattice`, `.edge_kappa` are compatibility copies of the four independent results. The legacy `call_prediction` field aliases `prediction.tool`; top-level `duration_p50_ms` and `duration_p90_ms` are rounded from ToolKB statistics. These compatibility fields do not select a winner among the four independent predictions. PMU remains a separate ToolKB prediction.

## Compatibility Prediction and Other Diagnostics

`prediction.tool_resource` is a compatibility diagnostic of the [tool decision schema](../contracts/tool-decision.schema.json); consumers should use `tool`, `trie`, or `lattice`:

| Field | Meaning |
| --- | --- |
| `repo`, `command`, `parse_failed`, `clause_bins` | Project, command, parse failure flag, and subcommand list. |
| `prediction`, `clause_predictions[]` | Overall / per-subcommand latency bucket predictions; sub-items are correlated using `clause_index`, `bin`, `argv`, `prediction`, `unavailable_reason`. |
| `bucket_id`, `probability_by_bucket`, `scope`, `key_kind`, `evidence_count`, `fallback_path` | Bucket index, bucket probabilities, evidence scope, match type, historical evidence count, and fallback path. |
| `unavailable_reason` | Reason the prediction is unavailable. |
| `continuous_predictions` | Historical conditional p90 for the five compatibility targets `latency_ms`, `cpu_peak_cores`, `sampled_peak_rss_bytes`, `memory_total_peak_bytes`, `memory_extra_peak_bytes`. Each target contains `target`, `conditional_p90`, `scope`, `key_kind`, `evidence_count`, `fallback_path`, `note`; it still does not include the cumulative CPU and average cores from the authoritative load protocol. |
| `lattice_time_predictions`, `lattice_resource_predictions` | Shrinkage, LOSO, and max-cardinality method results and resource distributions for subcommands; see the tables below for the fields. |
| `composed`, `composed_total_ms`, `composition` | Whether composed, the composed total time, and the `kind`, `bins`, `time_ms`, `dropped_viewer_bins` of each serial / pipeline group. |
| `prediction_algorithms` | List of algorithms and their input targets, outputs, and source descriptions. |
| `kv_ttl_cost` | KV cache retention policy simulation, not a measured cache metric of the model service. |
| `numa_usage` | Optional snapshot of host NUMA CPU usage at prediction time; it appears only when a sampler is configured and is not the resource attribution or placement execution result of the current tool. |

Sub-items of both lattice lists identify subcommands with `clause_index`, `bin`, `argv`, and `predictions[]` holds the results of each algorithm. The resource list additionally declares `scope`, `memory_metric`, `cpu_peak_window_ms`, `quantile_method`: attribution scope, environment memory definition, CPU peak window, and quantile method.

| `predictions[]` field | Meaning |
| --- | --- |
| `algorithm` | `shrinkage`, `loso`, or `max_cardinality`. |
| `prediction_ms` | Point prediction for the latency list, in ms; empty when unavailable. |
| `target`, `unit` | Target and unit for the resource list: cumulative CPU, average cores, peak cores, sampled RSS, environment memory total peak, or extra peak. |
| `p50`, `p90` | Empirical quantiles for the resource list, not confidence intervals. |
| `selected_features` | The final set of features used for matching. |
| `evidence_count` | Number of historical observations matched. |
| `selected_risk` | Risk score the algorithm uses to select candidates; it is not a failure probability and cannot be compared directly across algorithms. |
| `exact_match` | Whether it is an exact match; empty when not applicable. |
| `fallback` | Fallback description for the latency list. |
| `unavailable_reason` | Reason a valid prediction is missing. |
| `threshold`, `probability_ge` | Threshold for the resource list and the fraction of historical values greater than or equal to the threshold; the threshold has the same unit as the target, and it is not a calibrated probability of a future exceedance. |

`prediction_algorithms.enabled[]` contains `name`, `family`, `source`, `targets`, `outputs`, which are the algorithm name, family, source, targets, and outputs; `excluded[]` contains `name`, `source`, `reason`, describing algorithms that are not enabled and why.

`numa_usage.available` indicates whether the host provides `/proc/stat` and NUMA sysfs, and `sampled` indicates whether two samples are already available to compute a window difference; on the first read it can be available but not sampled. `node_count`, `window_s`, `user_hz`, `nodes[]` are the NUMA node count, sampling window in seconds, kernel tick frequency, and per-node data. Each node's `node`, `cpulist`, `online_cpus`, `cpu_utilization_pct`, `busy_cores` represent the node number, CPU list, online CPU count, busy percentage of the entire NUMA domain, and average busy cores within the window; 100% means all online CPUs of that node are fully loaded.

`kv_ttl_cost` contains `buckets_s`, `ttl_by_bucket_s`, `initial_bucket_index`, `final_bucket_index`, `num_bucket_jumps`, `bucket_exhausted`, `ttl_s`, `kv_eviction_time_s`, `kv_retention_time_s`, `reference_runtime_s`, `kv_cache_miss`, `miss_penalty_s`, `proxy_cost_s`: bucket boundaries / TTL (seconds), initial and final buckets, number of bucket jumps, bucket exhaustion flag, TTL, eviction time, retention time, reference duration, simulated misses, penalty, and proxy cost. It cannot prove the real cache hit rate.

`placement_advice.cpu_set`, `numa_node`, `llc_cluster`, `advisory` are the suggested CPU / NUMA / LLC placement and the advisory flag; the MVP does not promise actual CPU pinning. `decision_id`, `action`, `reason_code`, `reason`, `policy_name`, `policy_version`, `lease_id` are decision and lease fields, not resource measurements. `placement`, `profiling` are optional extensions.

## Service Aggregate Metrics

Prometheus service metrics aggregate multiple calls and are not a single-tool profile. The counters under the `scheduler_` prefix include `tool_requests_total`, `tool_decisions_total`, `tool_completions_total`, `tool_runtime_samples_total`, `tool_runtime_pid_samples_total`, `tool_runtime_unattributed_samples_total`, `tool_runtime_pid_unavailable_samples_total`, `sidecar_errors_total`, `calibration_updates_total`, counting requests, decisions, completions, samples, PID samples, unattributed samples, PID-unavailable samples, errors, and learning updates, respectively.

The resource cumulative counters are `tool_cpu_seconds_total`, `tool_io_read_bytes_total`, `tool_io_write_bytes_total`, `tool_net_rx_bytes_total`, `tool_net_tx_bytes_total`, `tool_context_switches_total`. They are currently accumulated in service memory and reset on restart; missing measurements are not backfilled, and they cannot be equated with the total resource usage of the whole machine.

Gauges include `active_leases`, `active_lease_millicores`, `active_tool_monitors`, as well as `tool_memory_rss_bytes`, `tool_memory_rss_peak_bytes`, `tool_process_count`, `tool_cpu_utilization_avg_cores`, `tool_io_read_bytes_per_second`, `tool_io_write_bytes_per_second`, `tool_net_rx_bytes_per_second`, `tool_net_tx_bytes_per_second`. The latter group reflects the most recent available completion sample; when a measurement is missing, the previous value may be retained, and they are not the instantaneous totals of all tools at the current moment.

`decision_latency_seconds`, `tool_duration_seconds`, `admission_wait_seconds` each provide `_count` and `_sum`, representing the sample count and total seconds of decisions, tool duration, and admission waits; they do not provide p50 / p90.
