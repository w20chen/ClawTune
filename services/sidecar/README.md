# ClawTune Sidecar

Strict eBPF telemetry is the supported default. For the complete host-package,
system-Python, root preflight, and OpenClaw workflow, follow the repository
[quick start](../../README.md#quick-start).

From this directory, install Sidecar dependencies into the selected
system-Python virtual environment:

```bash
python -m pip install -e '.[dev]'
```

Before starting the sidecar, run `tools/check_ebpf.py` from the repository
root as documented there. A plain unprivileged `python -m
clawtune_sidecar.main` is only suitable when eBPF telemetry has been explicitly
disabled for troubleshooting.

If editable install fails because the backend lacks `build_editable`, use:

```bash
python -m pip install '.[dev]'
```

Configuration uses environment variables:

- `CLAWTUNE_POLICY`
- `CLAWTUNE_MAX_GLOBAL_CONCURRENCY`
- `CLAWTUNE_LEASE_TTL_MS`
- `CLAWTUNE_ADMISSION_WAIT_MS`
- `CLAWTUNE_TOOL_RESOURCE_TRACES`
- `CLAWTUNE_TOOL_RESOURCE_EBPF_TRACES`
- `CLAWTUNE_TOOL_RESOURCE_LATENCY_BUCKETS_MS`
- `CLAWTUNE_TOOL_RESOURCE_TTL_BY_BUCKET_S`
- `CLAWTUNE_TOOL_RESOURCE_MISS_PENALTY_S`
- `CLAWTUNE_TOOL_RESOURCE_REPO`
- `CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR`
- `CLAWTUNE_TOOL_RESOURCE_CONTAINER_EXECUTABLE`
- `CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED`
- `CLAWTUNE_TOKEN`

`CLAWTUNE_CONFIG` is not consumed by the sidecar; use the environment
variables above.

When `CLAWTUNE_TOKEN` is set, start OpenClaw with the same value in
`CLAWTUNE_TOKEN` so both the plugin and `clawtune-launch` can authenticate
to the sidecar.

`CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED` defaults to `true`. In this
mode managed-wrapper `exec` claims fail closed unless native `tool_resource`
eBPF clause telemetry can be started before the payload command runs. Set it
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
