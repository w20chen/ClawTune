/** Producer for contracts/signal.schema.json. Public protocol lives in JSON Schema. */
export function eventSignal(value: unknown): Record<string, unknown> {
  const event = value as Record<string, unknown>;
  const type = event.event_type === "model_call_started" ? "model.started"
    : event.event_type === "model_call_ended" ? "model.completed" : "tool.completed";
  const identity = Object.fromEntries([
    "gateway_id", "runtime_id", "run_id", "session_id", "agent_id",
    "tool_call_id", "execution_id", "call_id",
  ].map(key => [key, event[key] ?? null]));
  identity.prediction_id = event.decision_id ?? null;
  return {
    schema_version: "clawtune.signal.v1", signal_id: event.event_id,
    type, source: "plugin", identity,
    observed_at: Date.parse(String(event.occurred_at)) / 1000,
    published_at: null, publisher_id: null, sequence: 0, expires_at: null,
    payload: event,
  };
}
