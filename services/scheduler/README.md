# Scheduler Sidecar

Strict Stage-2 eBPF is the supported default. For the complete host-package,
system-Python, root preflight, and OpenClaw workflow, follow the repository
[quick start](../../README.md#quick-start).

From this directory, install Scheduler dependencies into the selected
system-Python virtual environment:

```bash
python -m pip install -e '.[dev]'
```

Before starting the sidecar, run `tools/check_stage2.py` from the repository
root as documented there. A plain unprivileged `python -m
agent_scheduler.main` is only suitable when Stage-2 has been explicitly
disabled for troubleshooting.

If editable install fails because the backend lacks `build_editable`, use:

```bash
python -m pip install '.[dev]'
```

Configuration uses environment variables:

- `AGENT_SCHEDULER_POLICY`
- `AGENT_SCHEDULER_MAX_GLOBAL_CONCURRENCY`
- `AGENT_SCHEDULER_LEASE_TTL_MS`
- `AGENT_SCHEDULER_ADMISSION_WAIT_MS`
- `AGENT_SCHEDULER_TOOL_RESOURCE_TRACES`
- `AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_TRACES`
- `AGENT_SCHEDULER_TOOL_RESOURCE_LATENCY_BUCKETS_MS`
- `AGENT_SCHEDULER_TOOL_RESOURCE_TTL_BY_BUCKET_S`
- `AGENT_SCHEDULER_TOOL_RESOURCE_MISS_PENALTY_S`
- `AGENT_SCHEDULER_TOOL_RESOURCE_REPO`
- `AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR`
- `AGENT_SCHEDULER_TOOL_RESOURCE_CONTAINER_EXECUTABLE`
- `AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED`
- `AGENT_SCHEDULER_TOKEN`

`AGENT_SCHEDULER_CONFIG` is not consumed by the sidecar; use the environment
variables above.

When `AGENT_SCHEDULER_TOKEN` is set, start OpenClaw with the same value in
`OPENCLAW_SCHEDULER_TOKEN` so both the plugin and `claw-launch` can authenticate
to the sidecar.

`AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED` defaults to `true`. In this
mode managed-wrapper `exec` claims fail closed unless native `tool_resource`
Stage-2 clause telemetry can be started before the payload command runs. Set it
to `false` only for debugging coarse hook/PID/cgroup collection.

Runtime inspection:

```bash
curl http://127.0.0.1:8765/v1/tools/recent
curl http://127.0.0.1:8765/metrics
```

`/v1/tools/recent` returns the latest correlated OpenClaw tool runtime samples.
If OpenClaw provides `resource_scope.pid`, samples and metrics include
PID process-tree CPU, RSS, IO, and context-switch measurements. Without a PID,
the sample is explicitly marked `unattributed`.
