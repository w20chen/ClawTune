# Historical demo design

Status: superseded on 2026-09-11.

This file previously described the first three-path demo design. That design
was written before the unified multi-benchmark runner and included assumptions
that are no longer true, notably fixing benchmark width at one task and separate
benchmark-specific entry points. The old copy was also affected by character
encoding corruption, so retaining the full text made the documentation harder
to use without preserving reliable implementation guidance.

Use these current sources instead:

- [Documentation map](README.md)
- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [Current plan and validation](CURRENT_PLAN.md)
- [Multi-benchmark implementation](MULTI_BENCHMARK_IMPLEMENTATION.md)

The retained historical decision is only the high-level separation of three
workflows: persistent daily use, an online benchmark with a run-owned writable
knowledge base, and fixed-trace offline evaluation. Current benchmark
parallelism and asynchronous knowledge-base durability semantics are defined
by the documents above and by `contracts/benchmark-run.schema.json`.
