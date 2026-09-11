# Documentation map

## Run ClawTune

- [Installation and first run](getting-started.md): prerequisites, setup, daily use.
- [Online benchmarks](benchmarks.md): all five adapters, actual paths, dependencies and limits.
- [Offline evaluation](offline.md): fixed traces, units, task splits and results.
- [Configuration](configuration.md): provider keys, state ownership and runtime settings.
- [Troubleshooting](troubleshooting.md): failures and diagnostic artifacts.
- [ARM/QEMU](arm-qemu.md): Kunpeng container compatibility.

## Understand the implementation

- [Architecture](architecture.md)
- [Sidecar APIs and lifecycle](sidecar.md)
- [Trace and protocol reference](trace-schema.md)
- [Call-load prediction](call-load-prediction.md)
- [Lattice CPU and memory prediction](lattice-resources.md)
- [PMU profiling](pmu-profiling.md)
- [Current validation and limitations](CURRENT_PLAN.md)

JSON Schemas in [contracts](../contracts/) define the public protocol. CLI help
and checked-in configuration schemas define available options.

## Historical experiments

These retained reports describe fixed experiments, not the current CLI:

- [Legacy evaluator reproduction](legacy-eval.md)
- [Legacy evaluation results](legacy_eval_final_report.md)
- [Lattice accuracy snapshot](lattice-accuracy/report.md)

Superseded implementation plans and empty review logs are not operating guides;
Git preserves their history.
