# ClawTune OpenClaw Plugin

Connects model and tool lifecycle events to the local monitoring service. Installation and daily operation are documented in the [installation guide](../../docs/getting-started.md); options are defined in the [plugin schema](openclaw.plugin.json).

For manual development installation only, complete the main guide's dependency installation and build, then run from the repository root:

```bash
openclaw plugins install --link ./packages/clawtune-plugin
openclaw plugins enable clawtune
```

Manual linking does not configure collector privileges or model proxying.
