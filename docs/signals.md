# Unified signals

The sidecar publishes predictions and lifecycle notifications using one HTTP
POST protocol, defined by [signal.schema.json](../contracts/signal.schema.json).
Configure the **sidecar process**:

```bash
export CLAWTUNE_SIGNAL_ENDPOINT=http://127.0.0.1:8000/v1/signals
# Optional credential for the receiving service, separate from CLAWTUNE_TOKEN:
export CLAWTUNE_SIGNAL_TOKEN=receiver-credential
```

The receiver may use any path; it must accept the unified schema and return a
2xx response after accepting a message. The endpoint must point to the receiving
application, not back to ClawTune's observation endpoint. Restart the sidecar and
rebuild/restart the plugin after updating this checkout. No legacy GPU event
protocol, prediction environment variables, or progress files are used.

## Producers and control requests

The plugin sends model and tool completion observations to sidecar
`POST /v1/signals`. The POSIX managed launcher reports actual payload creation
and fresh tool progress to the same endpoint. The sidecar authenticates these
observations, associates them with its stored prediction, and publishes all
downstream messages through one asynchronous sender.

`POST /v1/decisions/tool` remains a synchronous control request. Its prediction
is calculated once; the same object is returned to the plugin and published as
`prediction.created`. Both contain the same [three-branch prediction](predictions.md):
`tool.load`, `trie.load`, and `lattice.load`, with PMU estimates only
under `tool`. `prediction_signal_id` in the response identifies that
publication; `decision_id` is the `prediction_id`. Execution registration binds
this prediction ID to the execution, including when no tool-call ID exists.
Execution registration, claim, cgroup/collector setup and exit acknowledgment
remain control operations under `/v2/executions`. The setup `/started` gate is
**not** proof that the payload has started. Only the supervisor's notification
after creating the payload produces `execution.started`.

The plugin authenticates using the existing sidecar bearer credential. The
observer uses the execution-specific update token in the Authorization header;
the sidecar derives ownership from the authenticated registration. The token is
removed before creating the user's process tree. External receiver credentials
stay in the sidecar. Producer messages cannot submit sidecar-owned predictions.

## Envelope

Every observation and publication has these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `clawtune.signal.v1` |
| `signal_id` | Unique message ID; unchanged on retry |
| `type`, `source` | Message kind and evidence producer (`plugin`, `launcher`, `sidecar`) |
| `identity` | Gateway, runtime, run, session, agent, tool call, model call, execution and prediction IDs; unavailable IDs are null |
| `observed_at` | Producer observation time in Unix seconds; for `execution.exited`, the launcher captures it immediately after observing process exit and before reporting to the sidecar |
| `published_at` | Sidecar publication time in Unix seconds; null on ingress |
| `publisher_id` | Sidecar process-lifetime UUID; null on ingress |
| `sequence` | Strictly increasing publication sequence within that publisher; producer sequence is not reused |
| `expires_at` | Unix expiry time for transient estimates; null for lifecycle facts |
| `payload` | Typed content selected by `type` |

Order is publication order, not a claim about simultaneous observations on
different hosts. A restarted sidecar gets a new publisher ID. Queue drops can
leave sequence gaps. Consumers deduplicate by `(publisher_id, signal_id)` and
must not let a late observation move an exited/completed execution back to
running. Clocks exchanging absolute observation times must be synchronized;
elapsed durations are measured entirely on the producer's monotonic clock.

Plugin ingress retains the typed model/completion payload used for measurement
and training. Egress tool completion carries a measurement summary; model egress
omits raw inputs/outputs. Neither credentials nor raw command output are added to
the unified downstream lifecycle messages.

## Signal types and rules

| Type | Trigger and content |
| --- | --- |
| `prediction.created` | One initial prediction per decision, including independent ToolKB, TrieKB and LatticeKB branches; PMU belongs to ToolKB; unavailable targets remain explicit |
| `execution.started` | Actual payload process creation, with PID and adapter name; independent of prediction availability |
| `execution.progress` | Ingress-only fresh remaining-time observation, elapsed time, source and `estimated`/`exact` kind |
| `prediction.updated` | At most once per execution when elapsed time is strictly greater than the original LatticeKB p50 + 1000 ms and fresh progress provides positive remaining time |
| `execution.finishing_soon` | At most once per execution when fresh progress reports `0 < remaining_ms <= 2000`; independent of historical duration predictions |
| `execution.exited` | Authenticated process exit, exit code or signal, timeout classification, and launcher-monotonic elapsed duration; elapsed duration is null only when setup failed before a payload start boundary existed |
| `execution.aborted` | Runtime cleanup confirms termination without an ordinary process exit report |
| `tool.completed` | Tool hook completion and call-level measurement summary; distinct from process exit |
| `model.started`, `model.completed` | Model request lifecycle, not internal GPU prefill/decode transitions |

Progress must advance in both observation time and monotonic elapsed time. The
sidecar rejects progress over two seconds old, over 250 ms in the future, after
termination, or past its expiry. A fresh estimate's validity is capped at the
smaller of two seconds and its reported remaining time. These are conservative
delivery limits, not accuracy guarantees. Old ETA observations are never counted
down to manufacture another observation.

An overrun update uses only an available LatticeKB duration p50 (unit `ms`, finite
and nonnegative); no ToolKB, TrieKB or p90 fallback applies. A zero baseline is
valid. The event references the original prediction and carries
`predicted_total_duration_ms = elapsed_duration_ms + remaining_duration_ms`,
marked `statistic=point_estimate`. This estimate is neither a new percentile nor
a training label. Finishing and overrun notifications are independent and can
come from the same observation. Crossing the baseline without progress sends
neither. A tool completion estimate does not predict when the next model request
will be submitted.

## Live progress coverage

| Tool | Evidence and restrictions |
| --- | --- |
| curl | Native Time Left; one explicit HTTP(S) URL, usable regular meter |
| wget | Native ETA; one explicit HTTP(S) URL, no recursive or input-list download |
| pytest / `python -m pytest` | Two advancing percentage updates over elapsed time; requires incremental output |
| ninja / `cmake --build` | Advancing completed/total counts or percentages |
| rsync | Overall `--info=progress2` ETA after file discovery stabilizes; not per-file progress |
| ffmpeg | Media duration minus current position, divided by speed; basic single-input/output transcodes |
| Other tools, generic scripts, pipelines and compound commands | No generic whole-call ETA; lifecycle facts still work on the supervised path |

Current adapters produce `estimated` only. `exact_remaining()` is an extension
interface, not an implemented exact-time guarantee. Test counts, build percentages
and media speeds need not predict final cleanup or heterogeneous work accurately.
The original command is not rewritten to enable progress. Silent tools and tools
requiring a TTY may provide no observation. The supervisor forwards output bytes
and termination signals; a parser failure disables progress without cancelling
the payload. The observer contributes a small amount of measured CPU/memory.

Supervision currently covers POSIX `managed-wrapper` executions. Windows,
`hook-only`, marker execution and external bridges retain their available
control/hook observations but do not acquire a fabricated payload-start or ETA
event. Registration failures in fail-open mode execute the tool without sending
unauthenticated observations.

## Delivery and diagnostics

The sidecar has one FIFO queue (default 1024 messages) and one sender. Each message
gets up to three attempts, retaining its identity and sequence. Only connection
errors, timeouts, HTTP 408/429 and 5xx responses are retried. Redirects are not
followed. Default per-attempt total timeout is one second; attempts for one message
share a five-second delivery window. Configure
`CLAWTUNE_SIGNAL_QUEUE_SIZE` and `CLAWTUNE_SIGNAL_TIMEOUT_S` if necessary.

Before each attempt, expired estimates and estimates for terminated executions
are discarded. An already in-flight request cannot be recalled; the receiver
must also check expiry and terminal state. Queue overflow drops the new message
and increments a counter. The observer has its own bounded 128-message queue and
at most two attempts to sidecar; progress is dropped after payload exit.

`GET /v1/signals/status` reports publisher identity, queue depth and published,
delivered, failed, expired and dropped counters. This endpoint uses the sidecar
bearer credential. Shutdown waits at most 1.1 seconds for the publisher before
cancelling pending delivery. Delivery is best effort: neither an ingress receipt
nor a successful decision promises external receipt. There is no durable outbox
or replay after a crash. Tool execution and KB learning do not await the receiver.

The tests cover schema validation, API correlation/authentication, retry identity,
strict thresholds, stale progress, terminal-state fences, bounded shutdown, real
curl/wget/pytest output, binary output preservation and process-group cancellation.
HTTP fixtures do not establish integration with an external GPU scheduler.
