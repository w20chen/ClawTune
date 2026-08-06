# Architecture

Runtime path:

```text
OpenClaw CLI / TUI / chat channel
  -> one Gateway (normal long-lived owner)
    -> agent
      -> session
        -> run (one submitted turn)
          -> agent-scheduler plugin hooks
            -> scheduler sidecar + eBPF collector
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

SWE-Rebench path:

```text
swe_rebench.runner
  -> generated /claw bundle
  -> Docker task container
  -> sidecar + plugin + openclaw agent --local
  -> swe_rebench/traces/<task_id>/*.jsonl
```

Deep Research Bench path:

```text
deep_research_bench.runner
  -> runtime bundle (plugin + scheduler + claw-launch)
  -> one very basic Docker sandbox image (python:3.11-slim by default)
  -> host sidecar + plugin + openclaw agent --local
  -> deep_research_bench/.runtime/traces/<task_id>/*.jsonl
```

Deep Research Bench has no per-task image or `/testbed` repository: the agent
answers a research question and its tools are measured with the
sandbox-container / per-PID scope, so its required-telemetry gate is relaxed
(LLM + resource-sampled tool spans, no Stage-2 exec clauses).

User guides:

- Getting started: [getting-started.md](getting-started.md)
- Configuration: [configuration.md](configuration.md)
- Sidecar: [sidecar.md](sidecar.md)
- ARM/QEMU: [arm-qemu.md](arm-qemu.md)
- Troubleshooting: [troubleshooting.md](troubleshooting.md)
- SWE-Rebench: [../swe_rebench/README.md](../swe_rebench/README.md)
- Deep Research Bench: [../deep_research_bench/README.md](../deep_research_bench/README.md)

Developer references:

- Public JSON Schemas: [`contracts/`](../contracts/)
- Event format implementation notes: [trace-schema.md](trace-schema.md)
- Current plan and validation: [CURRENT_PLAN.md](CURRENT_PLAN.md)
