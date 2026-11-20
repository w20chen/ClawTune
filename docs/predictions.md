# Pre-execution predictions

The public [prediction schema](../contracts/tool-prediction.schema.json) has
exactly three required, non-null peer branches: `tool`, `trie`, and `lattice`.
ToolKB is not a default or primary result. No model fills missing estimates
from another model. These branches separate model results, not transports.

The following is a structural illustration; `LoadPrediction` and
`PmuPrediction` stand for objects described below, not literal wire values:

```text
prediction
├── tool
│   ├── load: LoadPrediction
│   └── pmu: PmuPrediction
├── trie
│   └── load: LoadPrediction
└── lattice
    └── load: LoadPrediction
```

The [complete JSON example](../contracts/examples/tool-prediction.json) is
validated by `python tools/validate_contracts.py`. The synchronous
`POST /v1/decisions/tool` response uses `prediction`; the unified
`prediction.created` signal uses `payload.prediction`. Both carry the same
object, also recorded at `span_start.prediction`.

## Load predictions

Each branch's `load` follows [call_load.v2](../contracts/call-load.schema.json).
Its `targets` always contains all seven entries:

| Target | Unit | Meaning |
| --- | --- | --- |
| `duration_ms` | ms | Retained workload elapsed time |
| `cpu_time_seconds` | core_seconds | Owned workload CPU time |
| `cpu_avg_cores` | cores | CPU time divided by retained workload elapsed time |
| `cpu_peak_cores` | cores | Peak CPU consumption in fixed 500 ms windows |
| `sampled_peak_rss_bytes` | bytes | Sampled peak RSS across distinct address spaces |
| `memory_total_peak_bytes` | bytes | Peak total memory for the selected environment measurement |
| `memory_extra_peak_bytes` | bytes | Peak environment memory above its baseline |

Each target has `status`, `unit`, `metric_definition`, `avg`, `p50`, `p90`,
`buckets`, `backend`, `method`, `evidence_counts`, `sample_count`, `context`,
`assumptions`, `calibration`, and `unavailable_reason`. `buckets` contains
`edges`, `interval`, and `probabilities`. `evidence_counts` describes historical
evidence; `sample_count` can instead count synthetic composition samples.
All estimates currently declare `calibration: "unvalidated"`.

Available targets contain all statistics and a null `unavailable_reason`.
Unavailable targets retain their keys, units, bucket edges and source metadata,
but have null `avg`, `p50`, `p90`, and `buckets.probabilities`, zero
`sample_count`, `method: "unavailable"`, and a nonempty reason. Missing evidence
is not a prediction of zero. Clause estimates may be available even when a
whole-call composition is unavailable.

`load` also declares `schema_version`, `scope`, `lifecycle`,
`memory_measurement`, `cpu_peak_window_ms`, and `quantile_method`.
`clause_predictions` holds the subcommand estimates when present. Each entry
identifies its clause index, arguments, working directory, environment-variable
names, memory measurement and seven targets. Clause duration measures that
clause rather than the whole call. See [field semantics](tool-profile.md#prediction-span_startprediction).

ToolKB queries whole-call history. TrieKB and LatticeKB query their own clause
evidence and independently compose a whole-call estimate. Shared parsing does
not share or merge prediction evidence. The existing backend metadata names
are `runtime` for ToolKB, `trie` for TrieKB, and `lattice` for LatticeKB; schemas
reject a branch containing another backend's estimates.

## ToolKB-specific estimates

`tool.pmu` follows [pmu_prediction.v2](../contracts/pmu-prediction.schema.json),
using whole-execution hardware history. Its nine required targets are `cycles`,
`instructions`, `llc_read_accesses`, `llc_read_misses`, `ipc`, `llc_mpki`,
`llc_miss_rate`, `llc_read_accesses_per_cpu_second`, and
`llc_read_misses_per_cpu_second`. Each has availability, units, measurement
definition, mean, p50, p90, source, method, `evidence_count`, context, calibration
and an unavailable reason. PMU estimates have no load histograms. TrieKB and
LatticeKB do not expose PMU estimates.

## Consumers and installation

Consumers must select a branch explicitly. Resource admission currently uses
CPU estimates from `tool.load`; progress-based overrun detection uses
`lattice.load.targets.duration_ms.p50`, with no fallback to another model.
The ClawBox bridge sends only the model explicitly selected by its configuration.
Policy, lease, identity, and explicit execution placement settings remain outside the
prediction object.

There are no top-level duration summaries, `resource_class`, `confidence`,
`call_prediction`, `pmu_prediction`, `diagnostics`, or `tool_resource` aliases.
Short/medium/long duration labels are not produced or carried by leases,
monitoring samples, or traces. Histograms and p50/p90 estimates remain available.
Old prediction fields are rejected, not silently translated. Update and restart
both the sidecar and built plugin together; the plugin requires the health capability
`clawtune.prediction.v1`.
