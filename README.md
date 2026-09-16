# ClawTune

ClawTune is an execution monitoring and resource prediction system for OpenClaw agents. It correlates model requests, tool calls, and operating-system measurements, then uses historical observations to estimate execution time, CPU use, and memory consumption before subsequent calls.

The system consists of a plugin and a local service; it does not modify OpenClaw core. It reports concurrency and resource information for deployment components.

| Path | Purpose | Main command | Read next |
| --- | --- | --- | --- |
| Daily OpenClaw operation | Monitor normal agent conversations and learn from completed tool calls | `openclaw gateway run` | [Installation and use](docs/getting-started.md) |
| Online benchmark | Execute benchmark tasks with a shared, run-local model that learns during the run | `python3 scripts/clawtune.py benchmark ...` | [Benchmarks and evaluation](docs/benchmarks.md#1-configure-a-first-run) |
| Offline evaluation | Train on existing execution traces and evaluate on held-out tasks without model calls | `python3 scripts/clawtune.py offline ...` | [Offline evaluation](docs/benchmarks.md#5-fixed-trace-offline-evaluation) |

Read the [technical report](docs/technical-report.md) for the system design, measurement definitions, prediction methods, and equations. Set up a new machine with the [installation guide](docs/getting-started.md) before using any live path.

The repository contains configuration templates, protocols, test fixtures, and a small initialization prior.

[JSON Schemas](contracts/) define the public protocol.

See the [tool profile field reference](docs/tool-profile.md) for all measured resource and prediction targets, sampling cadence, units, attribution boundaries, and unavailable values.
