# Architecture

## Knowledge base names

All documentation uses **ToolKB**, **TrieKB**, and **LatticeKB**. Together they
form the three-snapshot KB bundle used by daily use, online benchmarks and
fixed-trace evaluation.

| Name | Responsibility | Current Python class (legacy identifier) | Current snapshot filename |
| --- | --- | --- | --- |
| ToolKB | Whole-tool-call observations and predictions, including eligible PMU history | `RuntimeToolResourceKB` | `runtime-tool-resource-kb.json` |
| TrieKB | Clause observations retrieved through exact/prefix/bin matching | `ClauseResourceKB` | `clause-resource-kb.json` |
| LatticeKB | Clause time/resource predictions using lattice contexts | `LatticeTimeKB` | `clause-lattice-time-kb.json` |

The Python classes, filenames, schema identifiers and serialized backend keys
still use their existing code/protocol names. The table maps those identifiers
to the documentation names; it does not imply that code identifiers were renamed.

## Workflows

```text
Daily OpenClaw Gateway or local agent
  -> ClawTune plugin: lifecycle hooks and managed execution
  -> local sidecar: model proxy, collector, predictor and trace writer
  -> JSONL traces + three-snapshot persistent user KB

benchmark CLI -> task adapters -> bounded worker pool
  -> separate OpenClaw homes/workspaces and task backends
  -> one shared sidecar and run-owned online KB

offline CLI -> existing task traces -> grouped task split
  -> training seed -> frozen evaluation
```

The Gateway owns conversations; a local `openclaw agent --local` embeds its own
runtime. The plugin finalizes per-run trace/span state and can reuse a compatible
sidecar across sessions. Docker isolates tool execution; it does not own the
conversation. Model content is traced through the OpenAI-compatible sidecar
proxy at `/v1`, not by inspecting provider traffic elsewhere.

## Benchmark ownership

`benchmarks/adapters.py` normalizes input while preserving benchmark identity.
`benchmarks/runner.py` owns selection results, scheduling, run state and KB
lifetime. `benchmarks/runtime.py` routes repository tasks to the SWE host
executor, research to its web/sandbox executor, and BFCL/Terminal to native
backends behind an authenticated loopback tool bridge.

Each task has a stable digest directory and runtime identity. Only the
coordinator writes `run.json`. No official graders run. Adapter limitations
and actual input paths are documented in the [benchmark guide](benchmarks.md).

## Learning and durability

Accepted observations update in-memory prediction state under a KB lock.
One background writer coalesces persistence and atomically publishes the three
snapshots through `CURRENT`; storage retains current and preceding generations.
There is no SQLite KB. Daily state, each online run and each offline experiment
have distinct owners and do not merge automatically.

Task drains wait for runtime-local executions/finalizers without forcing a
KB flush. Once workers finish, the coordinator drains every real runtime and
then forces persistence. Failure leaves the durability flag false. Cancellation
signals workers before joining them; uncertain cleanup preserves ownership and
prevents unsafe resume. Exact concurrent learning interleaving is not promised.

Resource attribution is quality-gated. Repository exec paths require exclusive
cgroups and clause telemetry; research/bridged tools may only have eligible
latency observations. Missing labels remain unavailable. Admission metadata and
placement recommendations do not constitute a placement actuator in this MVP.

See [sidecar](sidecar.md), [protocol](trace-schema.md), [offline](offline.md) and
[current validation](CURRENT_PLAN.md) for the remaining boundaries.
