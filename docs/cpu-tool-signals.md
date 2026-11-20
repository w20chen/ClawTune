# CPU tool signals

Set `CLAWTUNE_GPU_SIGNAL_ENDPOINT` in the process running the OpenClaw plugin,
for example `http://127.0.0.1:8000/request-signals/cpu-events`. Rebuild the plugin
and restart the gateway after updating this checkout. No progress file is used;
`CLAWTUNE_TOOL_PROGRESS_PATH` is obsolete and ignored.

The existing `tool_start` and `tool_finishing_soon` eligibility rule is unchanged:
only `latency_long` predictions (the primary backend's p90 > 2000 ms) enable them.
The independent overrun update uses an available **LatticeKB p50** and covers
short, medium, long and primary-backend-unknown calls. It never falls back to
the primary backend, trie, or LatticeKB p90 when the LatticeKB p50 is unavailable.
The plugin reads `prediction.lattice.targets.duration_ms.p50`, requiring an
available target with backend `lattice`, unit `ms`, and a finite nonnegative value.
The supported execution path is `managed-wrapper` for instrumented POSIX shell tools.
On Windows, the launcher preserves the original command without signal supervision.
The plugin's default tool/host filters are `exec` and `gateway`; configure them
for your deployment. `hook-only`, `marker`, native in-process tools and ClawBox's
bridge are not automatically covered by this launcher implementation.

## Events and evidence

`tool_start` is emitted after the payload process is created. It includes
`predicted_duration_ms` (the existing p90) and `progress_adapter`.

`tool_finishing_soon` is emitted once, while the payload is still running, on
the first usable observation in `0 < remaining_seconds <= 2`. It includes:

- `remaining_duration_ms`: remaining seconds converted to milliseconds;
- `remaining_source`: the specific tool and observed progress field;
- `remaining_kind`: `exact` or `estimated`.

`tool_prediction_updated` is attempted once per execution, at the first usable
fresh progress observation for which:

```text
elapsed_ms > original_lattice_p50_ms + 1000
remaining_ms > 0, finite
predicted_total_duration_ms = elapsed_ms + remaining_ms
```

Elapsed time uses a monotonic clock starting at payload process creation. The
event carries `baseline_source=lattice`, `baseline_statistic=p50`,
`baseline_prediction_ms`, `overrun_threshold_ms=1000`, `elapsed_duration_ms`,
`remaining_duration_ms`, `predicted_total_duration_ms`, `remaining_source` and
`remaining_kind`, plus the same execution/session identity and observation time
as the other events. The current-progress prediction is remaining wall time,
not a fabricated percentage of work completed. For example, a 5000 ms baseline,
6200 ms elapsed and a fresh 4000 ms ETA produce a 10200 ms predicted total.
That total is an estimate for this execution, not a new p50/p90 or a KB training label.

Merely crossing the deadline does not produce a prediction: an unsupported or
silent tool sends no update, and an old ETA is not counted down or reused as a
new observation. A zero LatticeKB p50 is valid and gives a 1000 ms threshold.
The overrun update is independent of the finishing notification: either may
precede the other, and both may arise from the same observation. If a call is
eligible only through LatticeKB, it sends only the overrun update, without adding
start/finishing notifications to the existing policy. Neither update changes
the original baseline or schedules repeated overrun updates.

The public event contract is [cpu-tool-signal.schema.json](../contracts/cpu-tool-signal.schema.json).
Identity and transport retain schema v1 (`request_id`, `engine_request_id`,
`agent_request_id`, `session_id`, `t`, `engine_id`, `role`, `engine_step`). The GPU
receiver must include the CPU event route introduced with this feature.

`ToolProgress.exact_remaining()` is the empty interface for a tool-specific API
that can establish actual remaining wall time. It takes priority over ETA when
available. **None of the current general-purpose CLI adapters claims exact
remaining wall time.** Percent, throughput, a requested sleep duration and a
timeout cannot establish that guarantee. The current adapters use live tool
progress and label their results `estimated`; they never count down from the
historical prediction. Updates can skip the window; no signal is invented on exit.

## Tool-specific adapters

| Tool / supported invocation | Live observation | Limits |
| --- | --- | --- |
| curl, one explicit HTTP(S) URL | Native regular meter's Time Left | No silent/disabled meter, batches, config-file URLs or URL globbing |
| wget, one explicit HTTP(S) URL | Native dot/bar ETA | No recursive/mirror/input-list downloads; ignore 100% elapsed-time rows |
| pytest, including `python -m pytest` | Change in reported test progress percentage over observed wall time | Needs incremental percentage output, e.g. `-v`; teardown/reporting and unequal tests affect ETA |
| ninja | Change in native `[completed/total]` updates | Custom `NINJA_STATUS` may remove the usable counts |
| `cmake --build` | CMake percentages or Ninja counts | Only the recognizable output formats; heterogeneous tasks affect ETA |
| rsync with `--info=progress2` | Overall native ETA after file discovery stabilizes | `xfr#` rows contain elapsed time and are ignored; `--progress` alone is per-file |
| ffmpeg, one input/output, basic transcode options | Input duration minus current media position, divided by current speed | Seek, trim, duration-changing filters, multi-output and unrecognized options are not adapted |
| pip, git, npm/yarn/pnpm, cargo, arbitrary Python scripts, generic make | Separate empty adapter classes | Their visible subtask bars do not establish whole-call progress; extension points remain empty |
| Other tools, shell pipelines, compound commands and shell redirections | Empty `ToolProgress` interface | A subcommand ending must not be mistaken for the entire call ending |

Arguments are inspected only to select an adapter; the user's command is not
rewritten to enable progress. A command suppressing its progress may therefore
send only `tool_start`. Generic Python/tqdm loops may be nested or followed by
more work, so the Python interface intentionally does not treat any bar as the
whole script. Add a script-specific adapter only when its progress scope is known.

## Execution and diagnostics

The launcher retains its existing registration/cgroup gate, then runs an internal
supervisor in the same execution scope. That supervisor starts the original shell
command, parses its native progress stream and forwards stdout/stderr bytes.
The extra supervisor contributes a small amount of CPU/memory to the measured
process tree. Output is piped for supported adapters: tools which only display
progress on a TTY may require their own force-progress option. No PTY is fabricated.

Each execution has its own adapter state and at most one ending notification.
SIGINT/SIGTERM/SIGHUP are forwarded to the payload process group. The launcher
retains the distinction between a normal exit code and termination by a signal.
Signal HTTP failures are logged and do not cancel or replay the tool. Waiting
for delivery at shutdown is bounded to 1.1 seconds; undelivered events may be lost.
A failed delivery is not retried. A faulty progress parser is disabled while
output forwarding and tool execution continue. Silent/unavailable progress is
reported on stderr; tools still run.
Queued finishing and overrun events are dropped if the payload exits before
their delivery attempt. The receiving service must accept `tool_prediction_updated`
to use the new estimates; older event types keep their existing fields.

## Verification

From the repository root, with the sidecar development dependencies installed:

```bash
python -m pytest services/sidecar/tests/test_tool_progress.py \
  services/sidecar/tests/test_tool_signals.py \
  services/sidecar/tests/test_launcher.py -q
```

The process tests run real curl/wget transfers against a local rate-limited HTTP
server, and a real pytest workload. They exercise both launcher modes, preserve
binary download results, and record signals plus actual exit observations. The
receiver and sidecar registration are HTTP test fixtures, not a native GPU
simulation or kernel-collector validation. The pre-execution prediction is set
to 999999 ms, deliberately unrelated to those workloads. Tests also verify that
an obsolete progress file cannot cause a signal, missing eligibility sends
nothing, and HTTP delivery failure leaves tool execution intact.
Additional real pytest workloads exceed a deliberately small LatticeKB baseline
and validate exactly one overrun update in both launcher modes, with and without
legacy long-call notifications. Boundary tests cover the strict one-second
threshold, unavailable baselines, stale/missing progress, and events queued at exit.

The `exact` interface is unit-tested as an extension contract only; that test
does not establish an exact-time implementation for any real tool.

Progress format references: [curl](https://curl.se/docs/tutorial.html),
[wget](https://www.gnu.org/software/wget/manual/html_node/Download-Options.html),
[pytest](https://docs.pytest.org/en/stable/how-to/output.html),
[rsync's progress implementation](https://github.com/RsyncProject/rsync/blob/master/progress.c).
