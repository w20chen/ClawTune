# SWE-Rebench integration

The supported command is `python3 scripts/clawtune.py benchmark --benchmark swe-rebench`.
Start with [installation](../docs/getting-started.md), then follow the
[SWE-Rebench section](../docs/benchmarks.md#swe-rebench) for task fields and commands.
The [shared benchmark guide](../docs/benchmarks.md#shared-controls-and-results)
covers selection, concurrency, timeouts, reports and resume.

This directory retains task discovery and host/Docker helpers used by the
unified runner. Its `runner.py`, replay helpers and `config*.yaml` are legacy
internal interfaces; use `configs/benchmark.yaml` and the public command for
new runs. Discovery output must be passed explicitly with `--dataset`.
