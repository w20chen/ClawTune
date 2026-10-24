import {isRecord} from "./config.js";

export function extractToolTimeout(event: unknown, result: unknown): {
  confirmed_timeout: boolean;
  timeout_duration_lower_bound_ms: number | null;
} {
  const record = isRecord(event) ? event : {};
  const output = isRecord(result) ? result : {};
  const details = isRecord(output.details) ? output.details : {};
  const reason = details.exitReason ?? details.exit_reason;
  const error = String(record.error_type ?? record.errorType ?? "").toLowerCase();
  const confirmed = reason === "overall-timeout" || reason === "no-output-timeout"
    || error.includes("timeout") || error.includes("timed out");
  if (!confirmed) return {confirmed_timeout: false, timeout_duration_lower_bound_ms: null};
  for (const source of [record, details]) {
    for (const [key, scale] of [["timeout_duration_lower_bound_ms", 1], ["timeoutMs", 1],
      ["timeout_ms", 1], ["timeoutSec", 1000], ["timeoutSeconds", 1000]] as const) {
      const value = source[key];
      if (typeof value === "number" && Number.isFinite(value * scale) && value > 0) {
        return {confirmed_timeout: true, timeout_duration_lower_bound_ms: value * scale};
      }
    }
  }
  // OpenClaw's exec runtime appends its effective timeout to this diagnostic.
  // Require a structured timeout outcome; ordinary command output is not proof.
  if (reason === "overall-timeout" && Array.isArray(output.content)) {
    for (const item of output.content) {
      if (!isRecord(item) || typeof item.text !== "string") continue;
      const match = /(?:^|\n)Command timed out after (\d+(?:\.\d+)?) seconds\./.exec(item.text);
      if (match && Number(match[1]) > 0 && Number.isFinite(Number(match[1]) * 1000)) {
        return {confirmed_timeout: true, timeout_duration_lower_bound_ms: Number(match[1]) * 1000};
      }
    }
  }
  return {confirmed_timeout: true, timeout_duration_lower_bound_ms: null};
}

export function extractToolExitCode(value: unknown, toolName: string): number | null {
  void toolName;
  if (typeof value === "number" && Number.isInteger(value)) return value;
  if (!isRecord(value)) return null;
  const direct = extractInteger(value, ["exit_code", "exitCode"]);
  if (direct !== null) return direct;
  const details = value.details;
  if (isRecord(details)) {
    return extractInteger(details, ["exit_code", "exitCode"]);
  }
  return null;
}

export function traceExitCodeForTool(toolName: string, statusCode: string, exitCode: number | null): number | null {
  if (exitCode !== null) return exitCode;
  if (toolName === "exec" && statusCode === "ok") return 0;
  return null;
}

function extractInteger(value: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const item = value[key];
    if (typeof item === "number" && Number.isInteger(item) && item >= 0) return item;
  }
  return null;
}
