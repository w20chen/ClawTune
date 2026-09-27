# ClawTune OpenClaw Plugin

Connects model and tool lifecycle events to the local monitoring service. Installation and daily operation are documented in the [installation guide](../../docs/getting-started.md); options are defined in the [plugin schema](openclaw.plugin.json).

For manual development installation only, complete the main guide's dependency installation and build, then run from the repository root:

```bash
openclaw plugins install --link ./packages/clawtune-plugin
openclaw plugins enable clawtune
```

Manual linking does not configure collector privileges or model proxying.

ClawBox hook-only deployments set `sandboxExecEnvelope: true` and
`sandboxExecPredictionModel: "lattice"` to send LatticeKB predictions to their
resource scheduler. The default model is `"tool"`. Missing or unavailable
predictions are passed through without substituting another model.
For a Cube guest, set the Sidecar's
`CLAWTUNE_TOOL_RESOURCE_MEMORY_MEASUREMENT=guest_memtotal_minus_memavailable`
to query matching environment-memory evidence. This selects the prediction
measurement; it does not convert cgroup or process RSS observations into guest labels.
