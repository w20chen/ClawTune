import {join} from "node:path";
import type {Logger} from "../logging.js";
import {stableDigest} from "../redaction.js";
import {CLOCK_PRECISION, CLOCK_SOURCE_DESCRIPTION} from "./clock.js";
import type {TraceMetadataRecord} from "./schema.js";
import {TRACE_SCHEMA_VERSION} from "./schema.js";
import {TraceWriter} from "./writer.js";

export type RunWriterScope = {
  traceDir: string;
  runtimeId: string;
  runId: string | null;
  sessionId: string | null;
  agentId: string | null;
};

type RunWriterEntry = {
  scope: RunWriterScope;
  writer: TraceWriter;
};

type PendingRunWriter = {
  scope: RunWriterScope;
  promise: Promise<RunWriterEntry>;
};

/**
 * Owns the plugin-side trace writers for one OpenClaw plugin registration.
 *
 * A Gateway can serve many runs for a long time. Keeping writers in a
 * process-wide map until Gateway shutdown leaks one file descriptor per run.
 * This manager makes run/session finalization explicit while retaining the
 * promise lock that prevents duplicate writers during concurrent hooks.
 */
export class RunWriterManager {
  private readonly entries = new Map<string, RunWriterEntry>();
  private readonly pending = new Map<string, PendingRunWriter>();

  constructor(
    private readonly flushSpanStart: boolean,
    private readonly logger: Logger,
  ) {}

  async get(scope: RunWriterScope): Promise<TraceWriter | null> {
    const runKey = scope.runId ?? scope.sessionId;
    if (!runKey) {
      this.logger.warn("trace: skipping write, no run_id or session_id available", {
        runId: scope.runId,
        sessionId: scope.sessionId,
        agentId: scope.agentId,
      });
      return null;
    }
    const key = writerKey(scope, runKey);

    const existing = this.entries.get(key);
    if (existing) return existing.writer;

    const inflight = this.pending.get(key);
    if (inflight) return (await inflight.promise).writer;

    const promise = this.createEntry(scope, key);
    this.pending.set(key, {scope, promise});
    try {
      return (await promise).writer;
    } finally {
      const current = this.pending.get(key);
      if (current?.promise === promise) this.pending.delete(key);
    }
  }

  /** Close every writer for one run, including agent-id variants. */
  async closeRun(scope: RunWriterScope): Promise<number> {
    if (scope.runId === null) return 0;
    return this.closeWhere((candidate) => (
      candidate.traceDir === scope.traceDir
      && candidate.runtimeId === scope.runtimeId
      && candidate.sessionId === scope.sessionId
      && candidate.runId === scope.runId
    ));
  }

  /** Close fallback/session writers when a session lifecycle ends. */
  async closeSession(scope: RunWriterScope): Promise<number> {
    if (scope.sessionId === null) return 0;
    return this.closeWhere((candidate) => (
      candidate.traceDir === scope.traceDir
      && candidate.runtimeId === scope.runtimeId
      && candidate.sessionId === scope.sessionId
    ));
  }

  async closeAll(): Promise<number> {
    return this.closeWhere(() => true);
  }

  /** Visible for regression tests and operational assertions. */
  activeWriterCount(): number {
    return this.entries.size;
  }

  /** Visible for regression tests and operational assertions. */
  pendingWriterCount(): number {
    return this.pending.size;
  }

  private async createEntry(scope: RunWriterScope, key: string): Promise<RunWriterEntry> {
    // Recheck after taking the pending slot. This also protects callers that
    // arrive between the initial entries lookup and pending registration.
    const existing = this.entries.get(key);
    if (existing) return existing;

    const filename = writerFilename(scope);
    const writer = new TraceWriter(
      join(scope.traceDir, filename),
      this.flushSpanStart,
      this.logger,
    );
    await writer.open();

    const metadata: TraceMetadataRecord = {
      schema_version: TRACE_SCHEMA_VERSION,
      record_type: "trace_metadata",
      trace_format_version: TRACE_SCHEMA_VERSION,
      scaffold: "openclaw",
      mode: "collect",
      created_at: new Date().toISOString().replace("+00:00", "Z"),
      clock_source: CLOCK_SOURCE_DESCRIPTION,
      clock_precision: CLOCK_PRECISION,
    };
    writer.writeRecord(metadata);

    const entry = {scope: {...scope}, writer};
    this.entries.set(key, entry);
    return entry;
  }

  private async closeWhere(
    matches: (scope: RunWriterScope) => boolean,
  ): Promise<number> {
    // Hook ordering guarantees agent_end/session_end after their model/tool
    // hooks, but writer creation itself contains async file I/O. Wait for any
    // matching creation already in flight before selecting handles to close.
    const inflight = Array.from(this.pending.values())
      .filter((entry) => matches(entry.scope))
      .map((entry) => entry.promise);
    await Promise.allSettled(inflight);

    const selected: RunWriterEntry[] = [];
    for (const [key, entry] of this.entries.entries()) {
      if (!matches(entry.scope)) continue;
      // Delete before awaiting close so a repeated lifecycle hook is
      // idempotent and cannot close the same handle twice.
      this.entries.delete(key);
      selected.push(entry);
    }
    await Promise.all(selected.map((entry) => entry.writer.close()));
    return selected.length;
  }
}

function writerKey(scope: RunWriterScope, runKey: string): string {
  return JSON.stringify([
    scope.traceDir,
    scope.runtimeId,
    scope.agentId,
    scope.sessionId,
    runKey,
  ]);
}

function writerFilename(scope: RunWriterScope): string {
  const session = safeFilename(scope.sessionId);
  const run = safeFilename(scope.runId);
  const identityDigest = stableDigest([
    scope.runtimeId,
    scope.agentId,
    scope.sessionId,
    scope.runId,
  ]).replace(/^sha256:/, "").slice(0, 12);
  return `${safeFilename(scope.runtimeId)}__${session}_${run}__${identityDigest}.jsonl`;
}

function safeFilename(segment: string | null): string {
  if (!segment) return "unknown";
  return segment.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 64);
}
