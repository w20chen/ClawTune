# CPU tool signals

Set `CLAWTUNE_GPU_SIGNAL_ENDPOINT` in the process running the OpenClaw plugin,
for example `http://127.0.0.1:8000/request-signals/cpu-events`. Rebuild the plugin
and restart the gateway after updating this checkout. No progress file is used;
`CLAWTUNE_TOOL_PROGRESS_PATH` is obsolete and ignored.

The existing eligibility rule is unchanged: only `latency_long` predictions
(p90 > 2000 ms) enable signals. Short, medium and unavailable predictions do not.
The supported execution path is `managed-wrapper` for instrumented shell tools.
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
SIGINT/SIGTERM/SIGHUP are forwarded to the payload process group. Signal HTTP
failures are logged and do not cancel or replay the tool. A failed delivery is
not retried. Silent/unavailable progress is reported on stderr; tools still run.

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

The `exact` interface is unit-tested as an extension contract only; that test
does not establish an exact-time implementation for any real tool.

Progress format references: [curl](https://curl.se/docs/tutorial.html),
[wget](https://www.gnu.org/software/wget/manual/html_node/Download-Options.html),
[pytest](https://docs.pytest.org/en/stable/how-to/output.html),
[rsync's progress implementation](https://github.com/RsyncProject/rsync/blob/master/progress.c).
