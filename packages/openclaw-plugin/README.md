# OpenClaw Agent Scheduler Plugin

This package is the OpenClaw plugin entrypoint. It reports model/tool hooks to
the scheduler sidecar and can instrument `exec` calls for stronger resource
attribution.

For the full user workflow, use
[../../docs/operator-guide.md](../../docs/operator-guide.md).

## Build

```bash
npm install
npm run build
npm test
```

## Install Into OpenClaw

From the repository root:

```bash
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable agent-scheduler
openclaw plugins inspect agent-scheduler --runtime --json
```

Expected hooks:

```text
before_tool_call
after_tool_call
model_call_started
model_call_ended
```

## Recommended Config

```bash
LAUNCHER_PATH="$(command -v claw-launch)"
test -n "$LAUNCHER_PATH"

cat <<JSON5 | openclaw config patch --stdin
{
  plugins: {
    entries: {
      "agent-scheduler": {
        enabled: true,
        config: {
          endpoint: "http://127.0.0.1:8765",
          mode: "observe",
          failOpen: true,
          recordRawTrace: true,
          executionBackend: "managed-wrapper",
          launcherPath: "$LAUNCHER_PATH",
          securityBoundaryAccepted: true
        }
      }
    }
  }
}
JSON5
```

`recordRawTrace` is disabled by package default. Enable it when you want
hook-visible tool args/results in traces. Use `managed-wrapper` when you want
the sidecar to correlate `exec` with a trusted PID or cgroup scope.
