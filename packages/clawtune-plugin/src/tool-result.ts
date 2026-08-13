import {isRecord} from "./config.js";

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
