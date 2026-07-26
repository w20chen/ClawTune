# Tool-resource SDK

`tool_resource` is a local command library: cold-start it from valid Stage-2
telemetry traces, then let it parse, query, predict, observe, and update around
Docker-owned execution. The package includes the eBPF collector and pinned
mvdan adapter source; it does not execute Docker or serve requests.

```python
from pathlib import Path

from tool_resource import DockerExecutionContext, LatencyBuckets, ToolResourceSDK

# Boundaries are explicit and have no SDK default.
sdk = ToolResourceSDK.from_traces(
    cold_start_telemetry_paths,
    LatencyBuckets(tuple(configured_latency_edges_ms)),
)
context = DockerExecutionContext(
    container_id=container_id,
    container_executable="docker",
    repo=repo,
    artifact_path=Path("command-clause-telemetry.json"),
)

run = sdk.start_command(context, tool_call_id, command)
actual = docker_runner.execute(command)
result = sdk.finish_command(run, actual, replay_execution="completed")
```

Cold start accepts valid artifacts independently, exposes accepted/rejected
artifact counts, eligible/withheld call coverage, and rejection reasons in
`sdk.cold_start_report`. It fails only when none of the supplied artifacts are
usable or no valid clause latency remains. Partial artifacts contribute only
their `eligible_for_kb=True` calls; withheld telemetry never enters the KB.

`run.prediction` is the pre-execution command bucket. `finish_command` first
finalizes the collector, then reads its artifact; only a completed replay with
valid collection, clean shutdown, intact telemetry, and an eligible command
enters the causal KB. The exact `actual` mapping is returned as
`result.workload_result`; telemetry failure cannot replace it.
`result.call_telemetry` is the per-command summary and
`result.telemetry_artifact` is the authoritative finalized artifact.
At artifact level, `replay_execution` is workload status,
`telemetry_quality` is collector health, and `formal_completeness` is
`complete`, `partial`, or `unavailable`; `call_coverage` reports the exact
eligible fraction.

The Docker runner owns execution, timeout, exit status, and container
lifecycle. The observer resolves the init PID and cgroup from the supplied live
container. Commands must descend from one long-lived in-container runner
process; independent `docker exec` roots have no trustworthy fork ancestry and
fail closed.

Bucket intervals are `[0, b1)`, `[b1, b2)`, ..., `[bk, +inf)`. A compound
command returns `compound_command_uncomposed`; bucket IDs are never ORed or
combined. CPU and memory measurements remain internal and are not part of the
public prediction contract.
