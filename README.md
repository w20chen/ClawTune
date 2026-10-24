# ClawTune

ClawTune is a standalone library and local service for monitoring tool execution and predicting resource use, with an OpenClaw plugin for agent integration. It correlates model requests, tool calls, and operating-system measurements, then uses historical observations to estimate execution time, CPU use, and memory consumption before subsequent calls.

Live collectors target Linux. The OpenClaw integration does not modify OpenClaw core, and resource-placement advice is advisory.

| Path | Purpose | Main command | Read next |
| --- | --- | --- | --- |
| Python library | Use prediction and knowledge-base modules without starting the agent integration | `python -m pip install ./services/sidecar` | [Library use](docs/getting-started.md#library-and-offline-use) |
| Daily OpenClaw operation | Monitor normal agent conversations and learn from completed tool calls | `openclaw gateway run` | [Installation and use](docs/getting-started.md) |
| Online benchmark | Execute benchmark tasks with a shared, run-local model that learns during the run | `python3 scripts/clawtune.py benchmark ...` | [Benchmarks and evaluation](docs/benchmarks.md#1-configure-a-first-run) |
| Offline evaluation | Train on existing execution traces and evaluate on held-out tasks without model calls | `python3 scripts/clawtune.py offline ...` | [Offline evaluation](docs/benchmarks.md#5-fixed-trace-offline-evaluation) |

Read the [technical report](docs/technical-report.md) for the system design, measurement definitions, prediction methods, and equations. Set up a new machine with the [installation guide](docs/getting-started.md) before using any live path.

Shell calls (`exec` and `terminal_exec`) support whole-call and subcommand-based
predictions. Other OpenClaw tools use whole-call history; their resource
measurements can include activity from the shared agent process. Offline accuracy
reports cover shell calls only, while training also includes non-shell calls.

New state starts with the bundled `seeds/bootstrap-v2` knowledge base, built from
14 tasks across 12 repositories. It includes the former test samples, so those
samples are no longer an independent evaluation set. Training retains partial
and approximate observations and fills missing numeric targets with zero;
complete training fields do not imply complete measurements. See
[state and initialization](docs/getting-started.md#3-output-and-persistent-state)
for seed selection and updates.

[JSON Schemas](contracts/) define the public protocol.

See [command rules](docs/command-parsing.md) for parsing inspection, automatic rule generation, and workspace configuration.

Start with the [trace measurement and prediction quick guide](docs/tool-profile.md#quick-guide-measurements-and-predictions), then use the same field reference for trace fields, units, attribution boundaries, and unavailable values.
