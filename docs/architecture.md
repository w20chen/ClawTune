# Architecture

Runtime path:

```text
OpenClaw CLI / TUI / chat channel
  -> one Gateway (normal long-lived owner)
    -> agent
      -> session
        -> run (one submitted turn)
          -> ClawTune plugin hooks
            -> ClawTune Sidecar + eBPF collector
              -> JSONL traces + SQLite state + recent metrics
```

For ordinary use, a single Gateway serves one user and a small number of
sessions. The Gateway and sidecar can remain alive, but plugin trace writers,
span registries, sequence counters, and parent mappings are finalized per run;
session-level cleanup is the fallback when a run ID is unavailable.

`openclaw agent --local` bypasses the Gateway and owns one embedded run. It is
useful for smoke tests and automation, not the default multi-turn CLI shape.
`openclaw chat` similarly uses an embedded runtime but keeps an interactive TUI
open for its process lifetime.

Docker sits beside this ownership chain rather than inside it. When OpenClaw
sandboxing is enabled, containers isolate tool execution; ClawTune correlates
their cgroups and processes back to the owning run. A Docker container is not
a session, and normal use does not require creating one container per turn.

Full LLM content is captured when OpenClaw uses the sidecar as an
OpenAI-compatible proxy:

```text
OpenClaw provider -> http://127.0.0.1:8765/v1 -> upstream LLM API
```

Online benchmark path:

```text
scripts/clawtune.py benchmark
  -> bounded worker pool + one run-owned sidecar/KB
  -> selected peer adapter
  -> OpenClaw + native task/tool backend
  -> .runtime/benchmarks/<benchmark>/<run>/
```

`parallelism=1` is serial; larger values bound tasks in flight. Tasks do not
wait at a shared barrier. Runtime-local finalizers drain as each task exits,
while the sidecar coalesces KB persistence asynchronously through one writer.
One global durability barrier runs after all producers finish.

Repository benchmark path:

```text
peer repository adapter
  -> task image export + OpenClaw Docker sandbox
  -> managed launcher + eBPF/cgroup telemetry
  -> run-owned traces and shared KB
```

Deep Research Bench instead uses a basic sandbox image and has no `/testbed`.
BFCL exposes native stateful functions through a tool bridge. Terminal Bench
copies and uses the task-owned Compose environment. These are user simulations,
not official leaderboard graders.

User guides:

- Getting started: [getting-started.md](getting-started.md)
- Configuration: [configuration.md](configuration.md)
- Sidecar: [sidecar.md](sidecar.md)
- ARM/QEMU: [arm-qemu.md](arm-qemu.md)
- Troubleshooting: [troubleshooting.md](troubleshooting.md)
- SWE-Rebench: [../swe_rebench/README.md](../swe_rebench/README.md)
- Deep Research Bench: [../deep_research_bench/README.md](../deep_research_bench/README.md)
- Offline evaluation: [legacy-eval.md](legacy-eval.md) ·
  [legacy_eval_final_report.md](legacy_eval_final_report.md)

Developer references:

- Public JSON Schemas: [`contracts/`](../contracts/)
- Event format implementation notes: [trace-schema.md](trace-schema.md)
- Current plan and validation: [CURRENT_PLAN.md](CURRENT_PLAN.md)
- Documentation map: [README.md](README.md)
