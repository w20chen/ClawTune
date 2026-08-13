export function sidecarRequestHeaders(): Record<string, string> {
  const headers: Record<string, string> = {"content-type": "application/json"};
  const token = process.env.CLAWTUNE_TOKEN;
  if (token) {
    headers.authorization = `Bearer ${token}`;
  }
  return headers;
}
