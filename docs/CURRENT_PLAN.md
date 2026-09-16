# Monitoring candidate for Linux acceptance (2026-09-16)

This working-tree revision supersedes the earlier eBPF-only policy. No OpenClaw
core, external dataset, benchmark adapter, or placement policy was changed.

- eBPF remains first choice for all tools. Collector failures may use a dedicated
  `exclusive-execution-cgroup` fallback. Two counter snapshots retain a baseline
  even if eBPF fails at completion. Shared runtime/container totals are never
  substituted for one tool. Sampling gaps alone do not trigger fallback.
- `resource_observation.backend`, `fallback_used`, `fallback_reason`, per-metric
  `measurement`, `available`, `eligible`, and `reason` describe the actual source.
  Cgroup CPU/IO values describe the collector window and are diagnostic, not
  action training labels. `memory_charge_peak` is separate from sampled RSS;
  it is emitted only when memory.peak increases beyond the starting high-water
  mark. An unchanged historical peak is not reused. No cgroup network value or
  unsampled CPU peak is invented.
- Missing/foreign action clocks cannot produce action resource labels. Execution
  summaries retain measured clause boundaries and compute interval-union coverage;
  they do not manufacture 100% coverage. Execution-only values remain visible but
  cannot train an action-wide average/peak without matching evidence.
- CPU/IO cumulative validity is separate from dense peak sampling. CPU execution
  totals use boundary differences, not raw lifetime exit counters. Each TID is
  checked independently. Regressing counters and sparse RSS remain ineligible.
- Duplicate/rebound windows release leases; rebinding opens the correct target.
  Completion runs outside the async event loop. The existing 30 ms ring delivery
  grace is still a heuristic, not a proof of loss-free draining; verify it on Linux.
- Unknown wall-clock monitor boundaries and sample counts are null. eBPF's 10 ms
  setting is nominal CPU-clock cadence, not a guaranteed wall-clock interval;
  cgroup fallback uses two snapshots and reports sampling_interval_ms=0.

## Deploy and test this candidate

Deploy plugin and sidecar from this same working tree. Rebuild the plugin with
`npm.cmd test` on Windows or `npm test` on Linux before packaging it.

For automatic fallback, leave `CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED` unset or set
it to `false`. Set it to `true` only for strict eBPF acceptance. Existing benchmark
runtime configuration may explicitly set `ebpf_required: true`; change that
runtime setting to `false` for the fallback acceptance run (adapter defaults were
not changed). Configure the existing delegated execution cgroup root if dedicated
per-execution cgroups are required; a shared container cgroup is insufficient.

On Linux run `python3 tools/check_ebpf.py`, then
`PYTHONPATH=services/sidecar/src python3 -m pytest services/sidecar/tests tests -q -rs`.
Neither live BCC/perf attach nor the Windows-skipped POSIX cases can be validated
on this Windows host. These commands must be repeated on the target kernel.

For normal and injected collector-failure runs, exercise CPU work, disk I/O,
allocate-then-sleep, concurrent calls, non-exec shared-runtime tools, and a short
call. Inspect each span's resource_observation, not just whether fields are non-null:

1. Normal collector: backend=ebpf, fallback_used=false.
2. Collector unavailable + dedicated cgroup: backend=cgroup-v2, fallback_used=true,
   explicit failure reason, nonnegative baseline deltas, collector-only labels.
3. Shared scope or missing baseline: unavailable/ineligible; no container total
   attributed to one tool. RSS never receives memory.current or memory.peak.
4. Long sleep: boundary-backed CPU/IO totals may be available; RSS/CPU peaks with
   insufficient sampling remain unavailable/ineligible. Short calls may be missing.
5. Missing clock, delayed scope, concurrent/retried calls: no invented complete
   coverage, cross-call counters, or leaked cgroup leases.

Final local validation:

- `PYTHONPATH=services/sidecar/src python -m pytest services/sidecar/tests tests -q --disable-warnings --tb=short --maxfail=3`: **983 passed, 14 skipped**.
- `npm.cmd test` in `packages/clawtune-plugin`: build passed; **104 tests passed**.
- Targeted Ruff checks on the monitoring implementation, telemetry/bridge, and new regression tests: passed.
- `python -m compileall -q services/sidecar/src`: passed.
- `git diff --check`: passed (Windows line-ending notices only).
- Draft 2020-12 validation: observation schema and call-load/tool-completed/tool-decision examples passed.
- Benchmark default-resource validation: `python scripts/clawtune.py benchmark --benchmark <name> --sample 1 --dry-run` passed for all five registered benchmarks; each resolved to a tracked task roster and tracked benchmark configuration.
- Root regression suite after the default-resource changes: `python -m pytest tests -q --disable-warnings --tb=short --maxfail=3` — **451 passed, 10 skipped**. `python tools/validate_docs.py` — **13 Markdown files, 37 local links; 0 errors**.

Linux action/kernel timestamp comparison assumes the same kernel monotonic clock
domain (including time namespace). Validate the actual deployment topology; a
clock-domain name alone does not establish synchronization between machines.

The sections below retain earlier environment acceptance notes.

# Outstanding Validation

- Timeout/cancellation audit: `PYTHONPATH=services/sidecar/src python3 -m pytest tests/test_timeout_finalization.py tests/test_benchmark_cancellation.py tests/test_benchmark_shutdown.py tests/test_benchmark_runtime_fixes.py tests/test_benchmark_terminal_exec.py services/sidecar/tests/test_runtime_abort.py -q -rs` still requires Linux for the eight subreaper/procfs/POSIX cases skipped on Windows. Live concurrent timeout acceptance additionally requires Linux, Docker, OpenClaw and provider credentials; exercise `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 --task-timeout-seconds 60` for each adapter, plus interruption during setup and tool execution, and verify no surviving producers and an acknowledged final KB flush.
  The targeted WSL variant using `tests/test_benchmark_shutdown.py tests/test_benchmark_terminal_exec.py tests/test_timeout_finalization.py tests/test_benchmark_cancellation.py services/sidecar/tests/test_runtime_abort.py` cannot run in the installed Ubuntu environment because `/usr/bin/python3` has no `pytest` module.

This file records checks that still require a suitable environment. Usage and configuration belong in the [installation guide](getting-started.md) and [benchmark guide](benchmarks.md).

The 2026-09-16 review of main commit `5e68953`, confirmed failure modes, implementation status, and validation plan are in [TRACE_MONITORING_REVIEW.md](TRACE_MONITORING_REVIEW.md). The eBPF-default redesign is implemented in the working tree; Linux kernel acceptance remains outstanding.

- Trace review Linux collector validation: `python3 tools/check_ebpf.py` cannot establish compile/attach/sampling behavior on this Windows host; run on the target Linux kernel with BCC, matching headers, Docker and required BPF/perf privileges. Include long-lived sleeping memory holders and shared-runtime non-exec tools in subsequent integration acceptance; passing command/exec preflight alone does not establish those capabilities.

- Fresh-OS package installation: `python3 scripts/clawtune.py setup` still needs acceptance on clean Ubuntu/openEuler images without preinstalled BCC/kernel tools. OpenClaw onboarding/Gateway/TUI acceptance also requires an interactive deployment.
- Real-provider acceptance across all benchmark adapters: `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 ...` can now use the tracked default rosters, but still requires provider credentials and the Linux runtime; the full matrix has not been rerun with this setup revision.
- The Ubuntu GitHub Actions workflow requires a remote CI run; local Windows checks are not equivalent.
- Linux/POSIX regressions: `PYTHONPATH=services/sidecar/src python -m pytest services/sidecar/tests tests -q -rs` needs Linux to exercise the launcher, subreaper, procfs/shell and file-mode cases skipped on Windows.
- Windows validation completed locally: `PYTHONPATH=services/sidecar/src python -m pytest services/sidecar/tests -q -rs` (all runnable tests pass; POSIX launcher tests are skipped). `npm.cmd test` in `packages/clawtune-plugin` also passes. These checks do not prove live eBPF attach, event delivery, loss accounting, or RSS coverage.
- `python -m ruff check services/sidecar/src services/sidecar/tests tests` was run but remains blocked by 33 pre-existing repository lint findings (mostly import ordering and test-style rules); no new lint baseline is introduced by the monitoring changes.
- `python -m ruff check benchmarks/adapters.py benchmarks/cli.py benchmarks/runtime.py scripts/benchmark_cache.py scripts/clawtune.py tests/test_benchmark_adapters.py` was run; it remains blocked by pre-existing findings in `benchmarks/runtime.py`, `scripts/benchmark_cache.py`, and an existing test line, so it is not a clean validation of this change.
- `python -m pytest -q` was run on 2026-09-16 after the timeout-finalization change; it could not collect the full repository suite on this Windows environment because the sidecar/vendor packages (`clawtune_sidecar`, `tool_time`, and the local `tool_resource` modules) are not installed on the active `PYTHONPATH`. The benchmark/timeout suites that are runnable here pass; run the documented Linux command below for the full suite.
- Live sampling acceptance: `python3 scripts/clawtune.py benchmark --benchmark terminal-bench --sample 3 --parallelism 3 ...` and the equivalent `--benchmark swe-rebench` can resolve the tracked smoke rosters but still require the Linux BCC/perf host and provider credentials. Full upstream acceptance additionally requires the corresponding custom datasets. Include both sub-second and multi-second tools; verify actual sample gaps, event-anchored timing, CPU window alignment/label availability, memory eligibility and exclusion of host service cgroups. Windows regression tests cannot establish live measurement accuracy.
- CubeSandbox end-to-end admission and delta restore: `clawbox --output-root /data/clawbox-results experiment run local.yaml --run-id clawtune-extra-memory` requires a patched ARM64 host and registered Runtime/Tool images; local Windows validation cannot exercise VM behavior.
