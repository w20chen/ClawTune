/**
 * Run-scoped span registry.
 *
 * Tracks active spans in memory so that:
 *  - span_end can look up span_start timestamps
 *  - duplicate ends are detected
 *  - incomplete spans can be identified at shutdown
 *  - tool-call-to-parent-LLM mapping is maintained
 */

import type {
  ActiveSpan,
  SpanKind,
} from "./schema.js";

type SpanKey = string; // `${runId}:${spanId}`
type ToolCallParent = {
  parentSpanId: string;
  identityKey: string | null;
};

export class SpanRegistry {
  /** active spans: keyed by runId:spanId */
  private spans = new Map<SpanKey, ActiveSpan>();
  /** tool_call_id → parent LLM spanId (per run) */
  private toolCallParents = new Map<string, ToolCallParent>();
  /** completed span keys (to detect duplicate ends) */
  private completed = new Set<SpanKey>();
  /** sequence number counter per run */
  private sequenceCounters = new Map<string, number>();

  // ── Span Lifecycle ─────────────────────────────────────────────────

  beginSpan(args: {
    identityKey?: string;
    traceId: string;
    spanId: string;
    parentSpanId: string | null;
    sessionId: string | null;
    runId: string | null;
    agentId: string | null;
    kind: SpanKind;
    name: string;
    startWallTimeNs: bigint;
    startMonotonicTimeNs: bigint;
  }): ActiveSpan {
    const runId = args.runId ?? args.traceId;
    const identityKey = args.identityKey ?? runId;
    const key = spanKey(identityKey, args.spanId);
    const sequenceNo = this.nextSequence(identityKey);

    const span: ActiveSpan = {
      traceId: args.traceId,
      spanId: args.spanId,
      parentSpanId: args.parentSpanId,
      sessionId: args.sessionId,
      runId,
      agentId: args.agentId,
      sequenceNo,
      kind: args.kind,
      name: args.name,
      startWallTimeNs: args.startWallTimeNs,
      startMonotonicTimeNs: args.startMonotonicTimeNs,
      startWritten: false,
    };

    this.spans.set(key, span);
    return span;
  }

  /** Mark a span_start as written to disk. */
  markStartWritten(runId: string, spanId: string): void {
    const span = this.spans.get(spanKey(runId, spanId));
    if (span) span.startWritten = true;
  }

  getSpan(runId: string, spanId: string): ActiveSpan | undefined {
    return this.spans.get(spanKey(runId, spanId));
  }

  /**
   * End a span. Returns the active span if it exists and hasn't been
   * ended before. Returns null for duplicate ends.
   */
  endSpan(runId: string, spanId: string): ActiveSpan | null {
    const key = spanKey(runId, spanId);
    if (this.completed.has(key)) return null; // duplicate end
    const span = this.spans.get(key);
    if (span) {
      this.completed.add(key);
      this.spans.delete(key);
    }
    return span ?? null;
  }

  /** List active spans globally or for one composite run identity. */
  listActiveSpans(identityKey?: string): ActiveSpan[] {
    if (identityKey === undefined) return Array.from(this.spans.values());
    const prefix = `${identityKey}:`;
    return Array.from(this.spans.entries())
      .filter(([key]) => key.startsWith(prefix))
      .map(([, span]) => span);
  }

  /**
   * Remove every run identity selected by the caller and return active spans
   * that still need a terminal record. Identity keys are opaque to the
   * registry; the plugin owns their Gateway/runtime/session structure.
   */
  clearRunsWhere(matches: (identityKey: string) => boolean): ActiveSpan[] {
    const identities = Array.from(this.sequenceCounters.keys()).filter(matches);
    const active = identities.flatMap((identityKey) => this.listActiveSpans(identityKey));
    for (const identityKey of identities) this.clearRun(identityKey);
    return active;
  }

  // ── Parent Mapping ─────────────────────────────────────────────────

  /** Register a tool_call_id → parent LLM spanId mapping. */
  setToolCallParent(
    toolCallId: string,
    parentLlmSpanId: string,
    identityKey: string | null = null,
  ): void {
    this.toolCallParents.set(toolCallId, {
      parentSpanId: parentLlmSpanId,
      identityKey,
    });
  }

  /** Look up the parent LLM spanId for a tool_call_id. */
  getToolCallParent(toolCallId: string): string | null {
    return this.toolCallParents.get(toolCallId)?.parentSpanId ?? null;
  }

  /** Remove a tool_call_id mapping (after the tool is done). */
  clearToolCallParent(toolCallId: string): void {
    this.toolCallParents.delete(toolCallId);
  }

  // ── Run Cleanup ────────────────────────────────────────────────────

  /** Clean up all state for a given runId. */
  clearRun(runId: string): void {
    const prefix = `${runId}:`;
    for (const key of this.spans.keys()) {
      if (key.startsWith(prefix)) this.spans.delete(key);
    }
    for (const key of this.completed) {
      if (key.startsWith(prefix)) this.completed.delete(key);
    }
    this.sequenceCounters.delete(runId);
    for (const [key, parent] of this.toolCallParents.entries()) {
      if (parent.identityKey === runId) this.toolCallParents.delete(key);
    }
  }

  /** Reset all state (for testing). */
  clear(): void {
    this.spans.clear();
    this.completed.clear();
    this.toolCallParents.clear();
    this.sequenceCounters.clear();
  }

  // ── Internal ───────────────────────────────────────────────────────

  private nextSequence(runId: string): number {
    const current = this.sequenceCounters.get(runId) ?? 0;
    const next = current + 1;
    this.sequenceCounters.set(runId, next);
    return next;
  }
}

function spanKey(runId: string, spanId: string): SpanKey {
  return `${runId}:${spanId}`;
}
