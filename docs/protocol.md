# Protocol Reference

Most users do not need this file. Run the project with:

- [operator-guide.md](operator-guide.md)
- [../swe_rebench/README.md](../swe_rebench/README.md)

Public protocol schemas live in `contracts/`.

Stage-2 eBPF command artifacts are described by
`contracts/clause-telemetry.schema.json`. Mapped executable clauses expose
wall-clock boundaries and a structured terminal status. Clauses that never
entered the runtime (for example a proven shell short-circuit) are represented
separately in `no_runtime_exec` with explicit status provenance.

Validate them:

```bash
python tools/validate_contracts.py
```

Main event families:

- `scheduler.v1` tool before/completed events
- model start/end events
- `scheduler.v2` managed execution registration and scope lookup
- schema v6 trace records written as JSONL
