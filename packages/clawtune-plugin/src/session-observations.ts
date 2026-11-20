import {randomUUID} from "node:crypto";

type RecordValue = Record<string, any>;
const record = (value: unknown): RecordValue =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
const string = (...values: unknown[]): string | null =>
  values.find(value => typeof value === "string" && value.length > 0) as string ?? null;

type Owner = {gatewayId: string; runtimeId: string; sessionId: string | null;
  sessionKey: string | null; runId: string | null; agentId: string | null};
type Tool = {tool_call_id: string; settled: boolean; foreground: boolean};
type Observation = {revision: number; observer_id: string; observer_started_at: number;
  session_key: string | null; parent_session_key: string | null;
  run_status: string; model_call_id: string | null; batch: {batch_id: string; tools: Tool[]} | null};
type Entry = {owner: Owner; data: Observation; retiredRuns: Set<string>; messages: Set<string>;
  messageSeq: number; terminal: boolean};
type Events = {
  onAgentEvent?(handler: (event: unknown) => void): () => void;
  onSessionTranscriptUpdate?(handler: (event: unknown) => void): () => void;
};

/** Captures control facts only. The sidecar owns phase/ETA derivation.
 * Complete snapshots are periodically resent, so a local disconnect or sidecar
 * restart does not require replaying a journal of intermediate transitions.
 */
export class SessionObservations {
  private entries = new Map<string, Entry>();
  private aliases = new Map<string, Entry>();
  private parents = new Map<string, string>();
  private pending = new Map<Entry, unknown>();
  private unsubscribe: Array<() => void> = [];
  private timer: ReturnType<typeof setInterval>;
  private sending: Promise<void> | null = null;
  private closed = false;
  private observerId = randomUUID();
  private observerStartedAt = Date.now() / 1000;

  constructor(private resolve: (event: unknown, context: unknown) => Owner,
    private send: (message: unknown) => Promise<void>, events?: Events,
    private foregroundExec = false) {
    if (events?.onAgentEvent) this.unsubscribe.push(events.onAgentEvent(e => this.lifecycle(e)));
    if (events?.onSessionTranscriptUpdate) this.unsubscribe.push(
      events.onSessionTranscriptUpdate(e => this.transcript(e)));
    this.timer = setInterval(() => {
      for (const entry of this.entries.values()) this.queue(entry, false);
    }, 5000);
    this.timer.unref();
  }

  private entry(event: unknown, context: unknown, allowNewRun = false): Entry | null {
    let owner = this.resolve(event, context);
    if (!owner.sessionId || !owner.runId) return null;
    const alias = owner.sessionKey ? this.aliases.get(owner.sessionKey) : undefined;
    // Some lifecycle/transcript sources carry only sessionKey. Resolve it to
    // the concrete sessionId instead of creating a second state machine.
    if (owner.sessionId === owner.sessionKey) {
      if (!alias) return null;
      owner = {...owner, sessionId: alias.owner.sessionId, runtimeId: alias.owner.runtimeId};
    }
    const key = JSON.stringify([owner.gatewayId, owner.runtimeId, owner.sessionId]);
    let entry = this.entries.get(key);
    if (!entry) {
      if (!allowNewRun || this.entries.size >= 10000) return null;
      entry = {owner, data: {revision: 0, observer_id: this.observerId,
        observer_started_at: this.observerStartedAt, session_key: owner.sessionKey,
        parent_session_key: this.parents.get(owner.sessionKey ?? "") ?? null,
        run_status: "active", model_call_id: null, batch: null},
        retiredRuns: new Set(), messages: new Set(), messageSeq: -1, terminal: false};
      this.entries.set(key, entry);
    } else if (entry.owner.runId !== owner.runId) {
      if (!allowNewRun || entry.retiredRuns.has(owner.runId!)) return null;
      entry.retiredRuns.add(entry.owner.runId!);
      // Keep terminal generations fenced; never guess identity from ID string contents.
      entry.owner = owner;
      entry.terminal = false;
      entry.data = {...entry.data, run_status: "active", model_call_id: null, batch: null};
    }
    if (owner.sessionKey) {
      entry.data.session_key = owner.sessionKey;
      this.aliases.set(owner.sessionKey, entry);
    }
    return entry;
  }

  model(event: unknown, context: unknown, started: boolean): void {
    const entry = this.entry(event, context, started);
    const e = record(event);
    const callId = string(e.call_id, e.callId, e.id);
    if (!entry || entry.terminal || !callId) return;
    if (started) {
      entry.data.run_status = "active";
      entry.data.model_call_id = callId;
      entry.data.batch = null; // Actual next call reconciles any missed tool completion.
    } else if (entry.data.model_call_id === callId) {
      entry.data.model_call_id = null;
    } else return;
    this.queue(entry);
  }

  beforeTool(event: unknown, context: unknown): void {
    const entry = this.entry(event, context);
    const e = record(event);
    const id = string(e.tool_call_id, e.toolCallId, e.id);
    const tool = entry?.data.batch?.tools.find(t => t.tool_call_id === id);
    if (!entry || !tool || tool.settled || entry.terminal) return;
    // exec's default yield is real. Only an explicit global process-tool deny
    // proves that process ETA also describes this logical invocation.
    tool.foreground = e.toolName === "exec" && this.foregroundExec;
    this.queue(entry);
  }

  afterTool(event: unknown, context: unknown): void {
    const entry = this.entry(event, context);
    const e = record(event);
    const id = string(e.tool_call_id, e.toolCallId, e.id);
    const tool = entry?.data.batch?.tools.find(t => t.tool_call_id === id);
    if (!entry || !tool || tool.settled || entry.terminal) return;
    const details = record(record(e.result).details);
    tool.settled = true; // Includes failures, denials, and background handles.
    if ((e.toolName ?? e.tool_name) === "sessions_yield" && details.status === "yielded") {
      entry.data.run_status = "yielded";
      entry.data.model_call_id = null;
    }
    this.queue(entry);
  }

  transcript(value: unknown): void {
    const e = record(value), target = record(e.target), message = record(e.message);
    const entry = this.aliases.get(string(e.sessionKey, target.sessionKey) ?? "");
    if (!entry || entry.terminal || message.role !== "assistant") return;
    const sessionId = string(target.sessionId, e.sessionId);
    if (sessionId && sessionId !== entry.owner.sessionId) return;
    const messageId = string(e.messageId, message.id);
    if (messageId && entry.messages.has(messageId)) return;
    if (typeof e.messageSeq === "number") {
      if (e.messageSeq <= entry.messageSeq) return;
      entry.messageSeq = e.messageSeq;
    }
    if (messageId) entry.messages.add(messageId);
    if (message.stopReason === "error" || message.stopReason === "aborted") return;
    const calls = Array.isArray(message.content)
      ? message.content.filter((c: any) => c?.type === "toolCall") : [];
    if (calls.some((c: any) => !string(c.id)) || calls.length > 1024) return;
    const ids = calls.map((c: any) => String(c.id));
    if (new Set(ids).size !== ids.length) return;
    entry.data.model_call_id = null;
    entry.data.batch = ids.length ? {
      batch_id: `${entry.owner.runId}:${messageId ?? randomUUID()}`,
      tools: ids.map(tool_call_id => ({tool_call_id, settled: false, foreground: false})),
    } : null;
    this.queue(entry);
  }

  lifecycle(value: unknown): void {
    const e = record(value), data = record(e.data);
    if (e.stream !== "lifecycle") return;
    const entry = this.entry(e, {}, data.phase === "start");
    if (!entry || entry.terminal) return;
    if (data.phase === "start") entry.data.run_status = "active";
    else if (["finishing", "end", "error"].includes(data.phase)) {
      entry.data.model_call_id = null;
      entry.data.run_status = data.yielded || entry.data.run_status === "yielded" ? "yielded"
        : data.phase === "finishing" ? "finishing"
        : data.aborted ? "cancelled" : data.phase === "error" ? "error" : "ended";
      if (data.phase !== "finishing") entry.terminal = true;
    } else return;
    this.queue(entry);
  }

  spawned(event: unknown, context: unknown): void {
    const e = record(event), c = record(context);
    const child = string(e.childSessionKey, c.childSessionKey);
    const parent = string(c.requesterSessionKey);
    if (!child || !parent) return;
    this.parents.set(child, parent);
    const entry = this.aliases.get(child);
    if (entry) {
      entry.data.parent_session_key = parent;
      this.queue(entry);
    }
  }

  endSession(event: unknown, context: unknown): void {
    const owner = this.resolve(event, context);
    const entry = this.aliases.get(owner.sessionKey ?? "");
    if (!entry || owner.sessionId !== entry.owner.sessionId) return;
    entry.terminal = true;
    entry.data.run_status = "closed";
    entry.data.model_call_id = null;
    this.queue(entry);
  }

  private queue(entry: Entry, changed = true): void {
    if (this.closed) return;
    if (changed) entry.data.revision++;
    if (!entry.data.revision) return;
    const o = entry.owner;
    this.pending.set(entry, {
      schema_version: "clawtune.signal.v1", signal_id: randomUUID(), type: "session.observation",
      source: "plugin", identity: {gateway_id: o.gatewayId, runtime_id: o.runtimeId,
        session_id: o.sessionId, run_id: o.runId, agent_id: o.agentId,
        tool_call_id: null, call_id: null, execution_id: null, prediction_id: null},
      observed_at: Date.now() / 1000, published_at: null, publisher_id: null,
      sequence: 0, expires_at: null, payload: structuredClone(entry.data),
    });
    this.flush();
  }

  private flush(): void {
    if (!this.sending && this.pending.size && !this.closed) {
      this.sending = this.drain().finally(() => {
        this.sending = null;
        this.flush();
      });
    }
  }

  private async drain(): Promise<void> {
    while (this.pending.size && !this.closed) {
      const [entry, message] = this.pending.entries().next().value!;
      this.pending.delete(entry);
      try { await this.send(message); } catch { /* Next heartbeat sends current state. */ }
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    clearInterval(this.timer);
    for (const unsubscribe of this.unsubscribe) unsubscribe();
    await this.sending;
  }
}
