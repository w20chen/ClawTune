# Limits

- The current policy observes and records; it is not a CPU optimizer yet.
- Placement advice is advisory unless the launcher can apply it.
- Resource attribution is best with `executionBackend: "managed-wrapper"`.
- Tools without a trusted PID/cgroup are still traced, but resource fields may
  be null or `unattributed`.
- In SWE-Rebench host sandbox mode, the sidecar watches Docker `exec_create`
  and `exec_start` events for native tools. When `ExecInspect` exposes a live
  host PID in time, it samples that PID and descendants. Very short tools can
  exit before inspection; those calls remain explicitly attributed to the
  shared OpenClaw sandbox cgroup rather than being presented as exact PID data.
- Docker exec children share their container cgroup. Exact native-tool
  attribution therefore uses process-tree counters; per-exec cgroup counters
  require OpenClaw or the runtime to create a dedicated child cgroup.
- Full LLM content requires routing OpenClaw through the sidecar proxy.
- Docker, real task images, and a valid LLM key are required for live
  SWE-Rebench runs.
