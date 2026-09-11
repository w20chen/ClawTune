# Deep Research Bench integration

Use `python3 scripts/clawtune.py benchmark --benchmark deep-research-bench`.
`python3 scripts/clawtune.py drb ...` is a compatibility alias.

The [research guide](../docs/benchmarks.md#deep-research-bench) covers the upstream
source, task JSON, Tavily/plugin setup, sandbox settings and a complete command
sequence. [Shared controls](../docs/benchmarks.md#shared-controls-and-results)
cover parallelism, timeouts, reports and resume.

This directory contains research task discovery and the host executor reused
by the common runner. Its standalone runner and `config*.yaml` are legacy
interfaces. New runs use `configs/benchmark.yaml`. Reference articles are
recorded beside traces, not supplied as live answers; no official grading runs.
