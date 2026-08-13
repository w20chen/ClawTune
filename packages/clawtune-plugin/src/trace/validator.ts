/**
 * Trace v6 validator.
 *
 * Validates a JSONL trace file and reports diagnostics.
 * Can be used programmatically or via a CLI script.
 */

import type {
  SpanStartRecord,
  SpanEndRecord,
  TraceMetadataRecord,
} from "./schema.js";
import { containsPossibleSecret } from "./sanitizer.js";

export interface ValidationResult {
  records: number;
  metadataRecords: number;
  spanStarts: number;
  spanEnds: number;
  completeSpans: number;
  incompleteSpans: number;
  unresolvedParents: number;
  duplicateEnds: number;
  endsWithoutStarts: number;
  invalidCoverageRatios: number;
  possibleSecretLeaks: number;
  durationMismatches: number;
  inconsistentSpanIdentity: number;
  errors: string[];
}

export function validateTrace(lines: string[]): ValidationResult {
  const result: ValidationResult = {
    records: 0,
    metadataRecords: 0,
    spanStarts: 0,
    spanEnds: 0,
    completeSpans: 0,
    incompleteSpans: 0,
    unresolvedParents: 0,
    duplicateEnds: 0,
    endsWithoutStarts: 0,
    invalidCoverageRatios: 0,
    possibleSecretLeaks: 0,
    durationMismatches: 0,
    inconsistentSpanIdentity: 0,
    errors: [],
  };

  const parsed: unknown[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line === "") continue;
    try {
      const obj = JSON.parse(line);
      parsed.push(obj);
    } catch {
      result.errors.push(`line ${i + 1}: invalid JSON`);
    }
  }

  result.records = parsed.length;

  // Separate records by type
  const starts = new Map<string, SpanStartRecord>();  // spanId -> start
  const ends = new Map<string, SpanEndRecord[]>();     // spanId -> ends
  const endedSpanIds = new Set<string>();

  for (const record of parsed) {
    if (typeof record !== "object" || record === null) continue;
    const r = record as Record<string, unknown>;

    // Check schema_version
    if (r.schema_version !== 6 && r.record_type !== undefined) {
      result.errors.push(`record has schema_version=${r.schema_version}, expected 6`);
    }

    // Check for possible secret leaks
    if (containsPossibleSecret(r)) {
      result.possibleSecretLeaks++;
    }

    if (r.record_type === "trace_metadata") {
      result.metadataRecords++;
      continue;
    }

    if (r.record_type === "span_start") {
      result.spanStarts++;
      const start = r as unknown as SpanStartRecord;
      if (start.span_id) {
        starts.set(start.span_id, start);
      }
      continue;
    }

    if (r.record_type === "span_end") {
      result.spanEnds++;
      const end = r as unknown as SpanEndRecord;

      // Track ends per span_id
      if (end.span_id) {
        const existing = ends.get(end.span_id) ?? [];
        if (existing.length > 0) {
          result.duplicateEnds++;
        }
        existing.push(end);
        ends.set(end.span_id, existing);

        // Mark as ended
        endedSpanIds.add(end.span_id);
      }

      // Validate parent
      if (end.kind === "tool" && end.parent_span_id === null) {
        result.unresolvedParents++;
      }

      // Validate coverage ratio
      if (end.resources?.coverage_ratio !== null && end.resources?.coverage_ratio !== undefined) {
        const ratio = end.resources.coverage_ratio;
        if (ratio < 0 || ratio > 1) {
          result.invalidCoverageRatios++;
          result.errors.push(
            `span ${end.span_id}: coverage_ratio=${ratio} out of [0,1]`,
          );
        }
      }

      // Validate duration non-negative
      try {
        const dur = BigInt(end.duration_ns ?? "0");
        if (dur < 0n) {
          result.errors.push(`span ${end.span_id}: negative duration_ns`);
        }
        // Check monotonic consistency
        if (typeof end.monotonic_time_ns === "string") {
          const endMono = BigInt(end.monotonic_time_ns);
          const startRec = starts.get(end.span_id);
          if (startRec && typeof startRec.monotonic_time_ns === "string") {
            const startMono = BigInt(startRec.monotonic_time_ns);
            const expected = endMono - startMono;
            if (expected < 0n) expected === 0n; // shouldn't happen
            const diff = dur > expected ? dur - expected : expected - dur;
            // Allow 1% tolerance for clock granularity
            const tolerance = expected / 100n;
            if (diff > tolerance && diff > 1_000_000n) {
              result.durationMismatches++;
              result.errors.push(
                `span ${end.span_id}: duration_ns=${dur} vs monotonic delta=${expected}`,
              );
            }
          }
        }
      } catch {
        result.errors.push(`span ${end.span_id}: invalid duration_ns`);
      }
    }
  }

  // Check completeness
  for (const spanId of starts.keys()) {
    if (endedSpanIds.has(spanId)) {
      result.completeSpans++;
    } else {
      result.incompleteSpans++;
    }
  }

  // Check ends without starts
  for (const spanId of ends.keys()) {
    if (!starts.has(spanId)) {
      result.endsWithoutStarts++;
    }
  }

  // Check span identity consistency
  for (const [spanId, endList] of ends.entries()) {
    const start = starts.get(spanId);
    if (!start) continue;
    for (const end of endList) {
      if (!spansMatch(start, end)) {
        result.inconsistentSpanIdentity++;
        result.errors.push(
          `span ${spanId}: start and end have inconsistent identity fields`,
        );
      }
    }
  }

  return result;
}

function spansMatch(start: SpanStartRecord, end: SpanEndRecord): boolean {
  return (
    start.trace_id === end.trace_id &&
    start.span_id === end.span_id &&
    start.parent_span_id === end.parent_span_id &&
    start.kind === end.kind &&
    start.sequence_no === end.sequence_no
  );
}

export function formatValidationSummary(result: ValidationResult): string {
  const lines: string[] = [
    "Trace validation summary",
    "",
    `records: ${result.records}`,
    `spans (from starts): ${result.spanStarts}`,
    `complete spans: ${result.completeSpans}`,
    `incomplete spans: ${result.incompleteSpans}`,
    `unresolved parents: ${result.unresolvedParents}`,
    `duplicate ends: ${result.duplicateEnds}`,
    `ends without starts: ${result.endsWithoutStarts}`,
    `invalid coverage ratios: ${result.invalidCoverageRatios}`,
    `possible secret leaks: ${result.possibleSecretLeaks}`,
    `duration mismatches: ${result.durationMismatches}`,
    `inconsistent span identity: ${result.inconsistentSpanIdentity}`,
    "",
  ];

  if (result.errors.length > 0) {
    lines.push("Errors:");
    for (const err of result.errors) {
      lines.push(`  - ${err}`);
    }
    lines.push("");
  }

  const hasWarnings =
    result.incompleteSpans > 0 ||
    result.unresolvedParents > 0 ||
    result.endsWithoutStarts > 0 ||
    result.inconsistentSpanIdentity > 0;

  const hasErrors =
    result.invalidCoverageRatios > 0 ||
    result.possibleSecretLeaks > 0 ||
    result.duplicateEnds > 0 ||
    result.durationMismatches > 0 ||
    result.errors.length > 0;

  if (hasErrors) {
    lines.push("INVALID");
  } else if (hasWarnings) {
    lines.push("VALID WITH WARNINGS");
  } else {
    lines.push("VALID");
  }

  return lines.join("\n");
}
