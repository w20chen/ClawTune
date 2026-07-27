export function sidecarRequestHeaders(): Record<string, string> {
  const headers: Record<string, string> = {"content-type": "application/json"};
  const token = process.env.OPENCLAW_SCHEDULER_TOKEN;
  if (token) {
    headers.authorization = `Bearer ${token}`;
  }
  return headers;
}
