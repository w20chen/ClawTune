# Architecture

Runtime path:

```text
OpenClaw agent
  -> agent-scheduler plugin hooks
  -> scheduler sidecar
  -> JSONL traces + SQLite state + recent metrics
```

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

- OpenClaw: [operator-guide.md](operator-guide.md)
- SWE-Rebench: [../swe_rebench/README.md](../swe_rebench/README.md)
