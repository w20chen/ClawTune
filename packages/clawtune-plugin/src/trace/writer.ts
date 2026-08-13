/**
 * Concurrent-safe JSONL trace writer.
 *
 * All trace writes go through a single writer instance that serialises
 * appends through a promise chain, preventing interleaved lines.
 *
 * span_start records are flushed (fsync) after write when configured.
 */

import { open } from "node:fs/promises";
import { existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type { TraceRecord } from "./schema.js";
import type { Logger } from "../logging.js";

export class TraceWriter {
  private queue: Promise<void> = Promise.resolve();
  private fileHandle: Awaited<ReturnType<typeof open>> | null = null;
  private closed = false;

  constructor(
    private readonly filePath: string,
    private readonly flushSpanStart: boolean,
    private readonly logger: Logger,
  ) {}

  /** Open the file handle (call once before writing). */
  async open(): Promise<void> {
    const dir = dirname(this.filePath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    // Use append mode ("a") instead of write ("w") to avoid truncating
    // trace data written by other components (e.g. the Python scheduler).
    // The scheduler is the primary trace writer; this writer is a fallback.
    try {
      this.fileHandle = await open(this.filePath, "a");
    } catch (err) {
      this.logger.warn("trace writer failed to open file", safeError(err));
    }
  }

  /** Enqueue a write of a single trace record. */
  writeRecord(record: TraceRecord): void {
    if (this.closed) return;
    this.queue = this.queue.then(() => this._writeRecordInternal(record));
  }

  /** Close underlying file handle. Returns promise that resolves when done. */
  async close(): Promise<void> {
    this.closed = true;
    await this.queue;
    if (this.fileHandle !== null) {
      try {
        await this.fileHandle.close();
      } catch {
        // best-effort
      }
      this.fileHandle = null;
    }
  }

  private async _writeRecordInternal(record: TraceRecord): Promise<void> {
    if (this.fileHandle === null) return;
    try {
      const line = JSON.stringify(record) + "\n";
      await this.fileHandle.write(line, null, "utf-8");
      if (
        this.flushSpanStart &&
        (record.record_type === "span_start" || record.record_type === "trace_metadata")
      ) {
        try {
          await this.fileHandle.sync();
        } catch {
          // fsync failure is non-fatal
        }
      }
    } catch (err) {
      this.logger.warn("trace writer write failed", safeError(err));
    }
  }
}

function safeError(err: unknown): { type: string; message: string } {
  if (err instanceof Error) return { type: err.name, message: err.message };
  return { type: "unknown", message: String(err) };
}
