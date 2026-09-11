# ClawTune

ClawTune is an execution monitoring and resource prediction system for OpenClaw agents. It correlates model requests, tool calls, and operating-system measurements, then uses historical observations to estimate execution time, CPU use, and memory consumption before subsequent calls.

The system consists of a plugin and a local service; it does not modify OpenClaw core. Resource placement recommendations are advisory.

- [Technical report](docs/technical-report.md): system design, measurement definitions, prediction methods and equations.
- [Installation and use](docs/getting-started.md): machine setup, configuration, daily operation, and development.
- [Benchmarks and evaluation](docs/benchmarks.md): task preparation, five benchmark adapters, offline evaluation, and output interpretation.

Start with the installation guide on a new machine. The repository contains configuration templates, protocols, test fixtures, and a small initialization prior. Generated experimental results belong in local output directories or external storage.

[JSON Schemas](contracts/) define the public protocol. Outstanding environment checks are recorded in [CURRENT_PLAN.md](docs/CURRENT_PLAN.md).
