# Scheduler Sidecar

Run:

```bash
python -m pip install -e '.[dev]'
export PYTHONPATH=src
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

If editable install fails because the backend lacks `build_editable`, use:

```bash
python -m pip install '.[dev]'
```

Configuration uses environment variables:

- `AGENT_SCHEDULER_DB_PATH`
- `AGENT_SCHEDULER_POLICY`
- `AGENT_SCHEDULER_MAX_GLOBAL_CONCURRENCY`
- `AGENT_SCHEDULER_LEASE_TTL_MS`
- `AGENT_SCHEDULER_ADMISSION_WAIT_MS`
- `AGENT_SCHEDULER_TOOL_RESOURCE_TRACES`
- `AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_TRACES`
- `AGENT_SCHEDULER_TOOL_RESOURCE_LATENCY_BUCKETS_MS`
- `AGENT_SCHEDULER_TOOL_RESOURCE_REPO`
- `AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR`
- `AGENT_SCHEDULER_TOOL_RESOURCE_CONTAINER_EXECUTABLE`
- `AGENT_SCHEDULER_TOKEN`

`AGENT_SCHEDULER_CONFIG` is not consumed by the sidecar; use the environment
variables above.

When `AGENT_SCHEDULER_TOKEN` is set, start OpenClaw with the same value in
`OPENCLAW_SCHEDULER_TOKEN` so both the plugin and `claw-launch` can authenticate
to the sidecar.

Runtime inspection:

```bash
curl http://127.0.0.1:8765/v1/tools/recent
curl http://127.0.0.1:8765/metrics
```

`/v1/tools/recent` returns the latest correlated OpenClaw tool runtime samples.
If OpenClaw provides `resource_scope.pid`, samples and metrics include
PID process-tree CPU, RSS, IO, and context-switch measurements. Without a PID,
the sample is explicitly marked `unattributed`.
