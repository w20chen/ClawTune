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

User guides:

- Getting started: [getting-started.md](getting-started.md)
- Configuration: [configuration.md](configuration.md)
- OpenClaw: [operator-guide.md](operator-guide.md)
- Sidecar: [sidecar.md](sidecar.md)
- Deployment: [deployment.md](deployment.md)
- ARM/QEMU: [arm-qemu.md](arm-qemu.md)
- SWE-Rebench: [../swe_rebench/README.md](../swe_rebench/README.md)

Developer references:

- Public JSON Schemas: [`contracts/`](../contracts/)
- Event format implementation notes: [trace-schema.md](trace-schema.md)
- Current engineering plan and validation history: [CURRENT_PLAN.md](CURRENT_PLAN.md)
