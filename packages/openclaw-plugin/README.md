# ClawTune OpenClaw plugin

This plugin connects OpenClaw lifecycle hooks and managed shell execution to
the ClawTune Scheduler sidecar. The root setup command builds, links, enables,
and configures it automatically:

```bash
python3 scripts/clawtune.py setup
```

That is the supported setup path. It configures automatic privileged sidecar
startup and waits before the first model request, so OpenClaw can run directly
through a Gateway/TUI or `openclaw agent --local`.

## Developer build

```bash
npm install
npm run build
npm test
npm run typecheck
```

## Manual plugin installation

For plugin development only:

```bash
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable agent-scheduler
```

The plugin configuration must use the local sidecar endpoint, managed-wrapper
execution, cgroup tracking, and explicit security-boundary acceptance.
`launcherPath` may be an absolute `claw-launch` path or empty for PATH lookup.
See
[`openclaw.plugin.json`](openclaw.plugin.json) for the schema and the root
[configuration guide](../../docs/configuration.md) for normal settings.

Automatic sidecar startup is disabled by default because the OpenClaw process
does not have the kernel privileges required by accepted eBPF collection.

This project does not modify OpenClaw core.
