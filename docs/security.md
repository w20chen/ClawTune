# Security Notes

- The plugin runs as a normal OpenClaw plugin.
- This project does not modify OpenClaw core.
- Use `mode: "observe"` unless you intentionally want enforcement behavior.
- `managed-wrapper` rewrites `exec` commands and therefore requires
  `securityBoundaryAccepted: true`.
- Do not store provider API keys in committed config files. Normal plugin runs
  should use the key already configured in OpenClaw; the sidecar forwards
  OpenClaw's `Authorization` header by default. Use `LLM_API_KEY` only for
  SWE-Rebench automation, and use
  `AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY_OVERRIDE` only for an intentional
  sidecar override.
