/**
 * Resource coverage calculator.
 *
 * Pure functions for computing resource monitoring coverage metrics.
 * These work with monotonic timestamps to avoid wall-clock skew issues.
 */

import type {
  AttributionStatus,
  CoverageReason,
  MonitorQuality,
} from "./schema.js";

export interface CoverageInput {
  /** Action start monotonic time (ns) */
  actionStartMonotonicNs: bigint;
  /** Action end monotonic time (ns) */
  actionEndMonotonicNs: bigint;
  /** Monitor start monotonic time (ns), or null if no monitoring */
  monitorStartMonotonicNs: bigint | null;
  /** Monitor end monotonic time (ns), or null if no monitoring */
  monitorEndMonotonicNs: bigint | null;
  /** Whether a PID/cgroup was available for monitoring */
  pidAvailable: boolean;
  /** Whether PID was registered late (after action start) */
  pidRegisteredLate: boolean;
  /** Whether the monitor stopped early */
  monitorStoppedEarly: boolean;
  /** Whether there was a monitor error */
  monitorError: boolean;
  /** Whether clock data is missing */
  clockDataMissing: boolean;
}

export interface CoverageResult {
  coverageDurationNs: bigint;
  actionDurationNs: bigint;
  coverageRatio: number | null;
  coverageReason: CoverageReason | string;
  quality: MonitorQuality;
  attributionStatus: AttributionStatus;
}

/**
 * Compute resource coverage metrics.
 *
 * coverage_duration_ns = max(0, min(actionEnd, monitorEnd) - max(actionStart, monitorStart))
 * coverage_ratio = coverage_duration_ns / action_duration_ns
 */
export function computeCoverage(input: CoverageInput): CoverageResult {
  const actionDurationNs =
    input.actionEndMonotonicNs > input.actionStartMonotonicNs
      ? input.actionEndMonotonicNs - input.actionStartMonotonicNs
      : 0n;

  if (!input.pidAvailable) {
    return {
      coverageDurationNs: 0n,
      actionDurationNs,
      coverageRatio: null,
      coverageReason: "pid_unavailable",
      quality: "unknown",
      attributionStatus: "unattributed",
    };
  }

  if (input.clockDataMissing) {
    return {
      coverageDurationNs: 0n,
      actionDurationNs,
      coverageRatio: null,
      coverageReason: "clock_data_missing",
      quality: "unknown",
      attributionStatus: "partially_attributed",
    };
  }

  if (input.monitorError) {
    return {
      coverageDurationNs: 0n,
      actionDurationNs,
      coverageRatio: null,
      coverageReason: "monitor_error",
      quality: "unknown",
      attributionStatus: "failed",
    };
  }

  if (
    input.monitorStartMonotonicNs === null ||
    input.monitorEndMonotonicNs === null
  ) {
    return {
      coverageDurationNs: 0n,
      actionDurationNs,
      coverageRatio: null,
      coverageReason: "clock_data_missing",
      quality: "unknown",
      attributionStatus: "partially_attributed",
    };
  }

  // Compute overlap
  const overlapStart =
    input.monitorStartMonotonicNs > input.actionStartMonotonicNs
      ? input.monitorStartMonotonicNs
      : input.actionStartMonotonicNs;
  const overlapEnd =
    input.monitorEndMonotonicNs < input.actionEndMonotonicNs
      ? input.monitorEndMonotonicNs
      : input.actionEndMonotonicNs;

  const coverageDurationNs =
    overlapEnd > overlapStart ? overlapEnd - overlapStart : 0n;

  // Ratio
  let coverageRatio: number | null = null;
  if (actionDurationNs > 0n) {
    coverageRatio = Number(coverageDurationNs) / Number(actionDurationNs);
    // Clamp to [0, 1]
    if (coverageRatio < 0) coverageRatio = 0;
    if (coverageRatio > 1) coverageRatio = 1;
  }

  // Determine quality and reason
  let quality: MonitorQuality;
  let coverageReason: CoverageReason | string;

  if (coverageRatio !== null && coverageRatio >= 0.99) {
    quality = "complete";
    coverageReason = "full_window";
  } else if (input.pidRegisteredLate) {
    quality = "partial";
    coverageReason = "pid_registered_late";
  } else if (input.monitorStoppedEarly) {
    quality = "partial";
    coverageReason = "monitor_stopped_early";
  } else if (coverageRatio !== null && coverageRatio > 0) {
    quality = "partial";
    coverageReason = "pid_registered_late"; // conservative default
  } else {
    quality = "unknown";
    coverageReason = "monitor_window_no_overlap";
  }

  // Attribution
  const attributionStatus: AttributionStatus =
    quality === "complete"
      ? "attributed"
      : coverageRatio !== null && coverageRatio > 0
        ? "partially_attributed"
        : "failed";

  return {
    coverageDurationNs,
    actionDurationNs,
    coverageRatio,
    coverageReason,
    quality,
    attributionStatus,
  };
}
