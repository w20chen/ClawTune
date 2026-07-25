# OpenClaw Plugin Usage

Build:

```bash
cd packages/openclaw-plugin
npm install
npm run build
```

Install:

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

Recommended config:

```json5
{
  endpoint: "http://127.0.0.1:8765",
  mode: "observe",
  failOpen: true,
  recordRawTrace: true,
  executionBackend: "managed-wrapper",
  launcherPath: "/absolute/path/to/claw-launch",
  securityBoundaryAccepted: true
}
```

Use `executionBackend: "hook-only"` for debugging when command rewriting is not
wanted.
