# ClawTune OpenClaw plugin

This plugin connects OpenClaw lifecycle hooks and managed shell execution to
the ClawTune Scheduler sidecar. The root setup command builds, links, enables,
and configures it automatically:

```bash
python3 scripts/clawtune.py setup
```

That is the supported setup path. Run an agent with automatic privileged
sidecar lifecycle through `python3 scripts/clawtune.py agent ...`. Use
`python3 scripts/clawtune.py sidecar` only for a long-lived process.

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

The plugin configuration must use the local sidecar endpoint, an absolute
`claw-launch` path, managed-wrapper execution, cgroup tracking, and explicit
security-boundary acceptance. See
[`openclaw.plugin.json`](openclaw.plugin.json) for the schema and the root
[configuration guide](../../docs/configuration.md) for normal settings.

Automatic sidecar startup is disabled by default because the OpenClaw process
does not have the kernel privileges required by accepted eBPF collection.

This project does not modify OpenClaw core.
