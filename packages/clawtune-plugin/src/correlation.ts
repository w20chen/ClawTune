type Entry = {
  toolCallId: string | null;
  decisionId: string | null;
  leaseId: string | null;
  executionId: string | null;
  expiresAt: number;
};

export class CorrelationMap {
  private readonly entries = new Map<string, Entry>();

  constructor(private readonly ttlMs: number, private readonly maxEntries: number) {}

  set(key: string | null, decisionId: string | null, leaseId: string | null, executionId: string | null = null, toolCallId: string | null = null): void {
    if (!key) return;
    this.sweep();
    if (this.entries.size >= this.maxEntries) {
      const first = this.entries.keys().next().value;
      if (first) this.entries.delete(first);
    }
    this.entries.set(key, {toolCallId, decisionId, leaseId, executionId, expiresAt: Date.now() + this.ttlMs});
  }

  take(key: string | null, toolCallId: string | null = null): Entry | null {
    if (!key) return null;
    this.sweep();
    const exact = this.entries.get(key) ?? null;
    if (exact !== null) {
      this.entries.delete(key);
      return exact;
    }
    if (toolCallId === null) return null;
    const matches = Array.from(this.entries.entries()).filter(
      ([, entry]) => entry.toolCallId === toolCallId,
    );
    if (matches.length !== 1) return null;
    const [matchedKey, entry] = matches[0];
    this.entries.delete(matchedKey);
    return entry;
  }

  clear(): void {
    this.entries.clear();
  }

  private sweep(): void {
    const now = Date.now();
    for (const [key, entry] of this.entries.entries()) {
      if (entry.expiresAt <= now) this.entries.delete(key);
    }
  }
}
