export function sidecarRequestHeaders(): Record<string, string> {
  const headers: Record<string, string> = {"content-type": "application/json"};
  // AGENT_SCHEDULER_TOKEN is the canonical name used by the Python sidecar.
  // OPENCLAW_SCHEDULER_TOKEN is a plugin-specific alias kept for backward
  // compatibility.  When both are set, OPENCLAW_SCHEDULER_TOKEN wins.
  const token = process.env.OPENCLAW_SCHEDULER_TOKEN
    || process.env.AGENT_SCHEDULER_TOKEN;
  if (token) {
    headers.authorization = `Bearer ${token}`;
  }
  return headers;
}
