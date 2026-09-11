# Documentation map

The documents are grouped by purpose so that historical design notes are not
mistaken for current behavior. JSON Schemas in [`contracts/`](../contracts/)
remain the public protocol source of truth.

## Start here

- [Installation and first run](getting-started.md)
- [Configuration](configuration.md)
- [Troubleshooting](troubleshooting.md)
- [Architecture](architecture.md)
- [Sidecar reference](sidecar.md)
- [Trace and protocol reference](trace-schema.md)
- [Kunpeng and arm64 hosts](arm-qemu.md)

## Current workflows

- [Peer benchmark adapters and input formats](MULTI_BENCHMARK_IMPLEMENTATION.md)
- [SWE-Rebench input and operation](../swe_rebench/README.md)
- [Deep Research Bench input and operation](../deep_research_bench/README.md)
- [Call-load prediction](call-load-prediction.md)
- [Lattice CPU and memory prediction](lattice-resources.md)
- [Tool-level PMU profiling](pmu-profiling.md)
- [Current limitations and validation](CURRENT_PLAN.md)

## Historical and experimental material

These files document earlier designs or fixed experiment results. They are
useful for provenance, but they do not define current CLI behavior:

- [Superseded three-path design](DEMO_SYSTEM_DESIGN.md)
- [Resource-lattice implementation plan](RESOURCE_LATTICE_PLAN.md)
- [Legacy evaluator reproduction guide](legacy-eval.md)
- [Legacy fixed evaluation report](legacy_eval_final_report.md)
- [Lattice accuracy snapshot](lattice-accuracy/report.md)

The short [review log](REVIEW_LOG.md) records policy-level review decisions;
detailed implementation history belongs in Git.

