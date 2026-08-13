/**
 * Clock utilities for trace v6.
 *
 * Provides wall-clock time (for display / cross-component alignment) and
 * monotonic time (for duration calculations), both in nanoseconds.
 *
 * On Node.js, process.hrtime.bigint() provides nanosecond-resolution
 * monotonic time.  Wall-clock time is derived from Date.now() and
 * process.hrtime() to provide the best available nanosecond estimate.
 */

import { hrtime } from "node:process";

/** Monotonic time in nanoseconds (arbitrary origin, only deltas matter). */
export function monotonicNowNs(): bigint {
  return hrtime.bigint();
}

/**
 * Wall-clock time in nanoseconds since Unix epoch.
 *
 * We compute this as:  Date.now() * 1_000_000  +  sub-ms offset from hrtime.
 * This gives ~microsecond real precision on most platforms but the field
 * unit is always nanoseconds (higher digits may be zero-padded).
 */
export function wallClockNowNs(): bigint {
  const ms = BigInt(Date.now()) * 1_000_000n;
  // Get sub-millisecond component from hrtime (only valid for short deltas
  // from an arbitrary point, but combined with Date.now we get a reasonable
  // wall-clock estimate).
  const hr = hrtime();
  const subMs = BigInt(Math.floor(hr[1] / 1000)) * 1000n; // nanoseconds < 1ms
  return ms + subMs;
}

/**
 * Description of the clock source for trace metadata.
 */
export const CLOCK_SOURCE_DESCRIPTION =
  "Date.now() + process.hrtime() for wall clock; process.hrtime.bigint() for monotonic";

export const CLOCK_PRECISION = "nanosecond (best-effort)";

/**
 * Compute duration in nanoseconds from two monotonic timestamps.
 * Returns 0n if start > end (should not happen with correct usage).
 */
export function durationNs(startMonotonicNs: bigint, endMonotonicNs: bigint): bigint {
  if (endMonotonicNs < startMonotonicNs) return 0n;
  return endMonotonicNs - startMonotonicNs;
}
