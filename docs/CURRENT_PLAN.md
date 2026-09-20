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
  they do not manufacture 100% coverage. Finalized owned execution values may
  train workload targets; average CPU retains the full tool-duration denominator.
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

## Unified benchmark task budget (2026-09-18)

- All five adapters now inherit the single 1,200-second default in `BatchConfig`; bundled configurations do not override it. Per explicit user direction, Terminal native agent limits are metadata only. Setup and every agent turn share one deadline. OpenClaw tool settings remain untouched.
- Removed independent BFCL/Terminal payload timers and the bridge request timer. The bridge uses Node HTTP without an implicit fetch headers deadline and accepts the runtime's AbortSignal. Terminal live builds consume the task budget. Task timeouts remain ordinary failed cases only after confirmed cleanup; uncertain cleanup remains fatal.
- Validation: with `$env:PYTHONPATH='services/sidecar/src'`, `python -m pytest tests services/sidecar/tests/test_runtime_abort.py -q -rs --disable-warnings --tb=short`: **475 passed, 10 skipped** in `tests` plus **11 passed** in `test_runtime_abort.py`. New `tests/test_benchmark_timeout.py` covers shared deadlines, multi-turn execution, obsolete/invalid configuration, setup timeout attribution, and cleanup failure. `npm.cmd test` in `packages/clawtune-plugin`: build and **104 tests passed**. `python tools/validate_docs.py` and `git diff --check`: passed.
- `python scripts/clawtune.py benchmark --benchmark <name> --sample 1 --dry-run` passed for all five adapters and reported `task_timeout_seconds: 1200` without timeout arguments.
- Required Linux validation remains unavailable here: `PYTHONPATH=services/sidecar/src python3 -m pytest tests services/sidecar/tests/test_runtime_abort.py -q -rs` must exercise the ten subreaper/procfs/POSIX/file-mode cases skipped on Windows. Live `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 1 --task-timeout-seconds 60` cannot run here because Linux, Docker, and OpenClaw are unavailable. On the target host, check timeout during setup and active tools, absence of surviving producers, and the final KB flush. Also run a Terminal task with native `max_agent_timeout_sec` below the harness budget to confirm it no longer truncates the simulation. These checks require working provider credentials.

## Timeout record preservation (2026-09-18)

- Follow-up fix from the timeout review: once a task timeout is recorded (agent or BFCL/Terminal worker returns exit code `124`), the post-agent deadline checks no longer run. `benchmarks/runtime.py`, `deep_research_bench/host_runner.py`, and `swe_rebench/host_openclaw.py` (`run_host_openclaw_task`) previously raised `TaskDeadlineExceeded` again in the result-collection phase and rewrote `task-timeout.json` from "task timed out after <budget>s" to a "result collection" message. The first record now stays authoritative, and the result-collection group (artifact cleanup, patch collection) is skipped once a timeout is recorded — the same work the previous flow performed, since every remaining check raised immediately after the deadline had already expired.
- Regression tests: `tests/test_benchmark_timeout.py::test_agent_timeout_record_is_not_overwritten_by_result_collection`, `tests/test_timeout_finalization.py::test_recorded_agent_timeout_survives_result_collection`, `tests/test_deep_research_bench_runner_inspection.py::test_research_timeout_record_is_not_overwritten_by_collection`. All three fail against `56cd52d` and pass with the fix.
- Documentation: `docs/benchmarks.md` now states that OpenClaw/model-side limits end a turn as an ordinary agent failure (only the harness deadline yields `124`/`scope: task`), that `0` disables every harness-owned bound so a stuck task can wait indefinitely, and that the first timeout record is authoritative. `docs/tool-profile.md` attributes the abort reason to the task timeout while noting historical `agent_timeout` values.
- Validation: `$env:PYTHONPATH='services/sidecar/src'`; `python -m pytest tests -q -rs` → **478 passed, 10 skipped**; `python -m pytest services/sidecar/tests -q -rs` → **533 passed, 4 skipped**; combined latest full-tree result **1011 passed, 14 skipped**. `python tools/validate_docs.py` and `git diff --check`: passed.
- Deliberately unchanged: `run_host_openclaw_replay_task` still rewrites its record with `scope: "replay"` when the replay deadline expires, because that mode label is intentional. Apply the same preservation rule there if replay stops using its own scope.
- Linux-only validation (subreaper/procfs/POSIX cases and live benchmark runs) remains outstanding as listed above and below.

## CI flake fix: supervisor reaping test race (2026-09-18)

- GitHub Actions run 35326665057 failed `tests/test_benchmark_shutdown.py::test_supervisor_reaps_detached_descendants[8-False]` with `ValueError: invalid literal for int() with base 10: ''`; the other 487 collected tests passed. The race is pre-existing and unrelated to the timeout-record change.
- Root cause was the probe script inside the test: `Path.write_text` creates the pid file before flushing its content, so the polling reader could observe a created-but-empty file under CPU contention (8 parallel supervisors plus detached children).
- Fix: the probe script now publishes the pid through a temporary file plus `os.replace` (atomic rename), and the reader waits for a parseable pid with a 5-second deadline instead of only file existence.
- Local validation (Windows): `python -m pytest tests/test_benchmark_shutdown.py -q -rs` → **5 passed, 5 skipped** (the four supervisor parametrizations and the other subreaper case are Linux-only); full `python -m pytest tests -q -rs` → **478 passed, 10 skipped**. `python -m py_compile tests/test_benchmark_shutdown.py`: passed, and the generated probe script was smoke-run against the new reader helper.
- Required Linux validation: WSL is not installed on this host, so rerun the failing job's command (`python -m pytest tests -q --basetemp .pytest-tmp-root`) and `python -m pytest tests/test_benchmark_shutdown.py -q -rs` on Linux to exercise `test_supervisor_reaps_detached_descendants`.

## Online run output documentation (2026-09-18)

- `docs/benchmarks.md` section 4 now documents the complete online run output map for all five benchmarks: run-level layout (`run.json`/`report.json`, `kb/`, `sidecar/`, `runtime-assets/`, `workspaces/`, `runtime/<task-digest>/openclaw-home/`, and the Terminal-only `terminal-environments/`/`terminal-logs/`), per-task files with their adapter differences (repository artifacts, research `reference_answer.txt`/`web-search-config.log`, bridged `turn-<n>.txt`/`turn-<n>-agent-stderr.txt`, Terminal preflight), and the deliberately quiet console behavior. The stale `agent-stdout.txt` sentence in section 1 was corrected: host runs retain agent stderr only.
- Windows validation: `python tools/validate_docs.py` — **12 Markdown files, 36 local links; 0 errors** — and `git diff --check` passed. The file map was derived from the runtime code (`benchmarks/runner.py`, `benchmarks/runtime.py`, `swe_rebench/host_openclaw.py`, `swe_rebench/prepare.py`, `deep_research_bench/host_runner.py`, `benchmarks/backends.py`).
- Linux-only confirmation remains: run one task per adapter (`python3 scripts/clawtune.py benchmark --benchmark <name> --sample 1`) and verify the documented files appear at the documented paths with the documented semantics, especially bridged per-turn logs, the Deep Research Bench `openclaw-home/` placement inside its trace directory, and the Terminal `terminal-environments/`/`terminal-logs/` directories.

## Timeout configuration review against `75fec4c` (2026-09-18)

- Windows review validation: with `$env:PYTHONPATH='services/sidecar/src'`, `python -m pytest tests -q -rs --disable-warnings --tb=short` passed (455 passed, 10 skipped), and `python -m pytest services/sidecar/tests/test_runtime_abort.py -q -rs` passed (11 passed). `git diff --check` passed.
- The ten skipped root tests still require Linux subreaper/procfs/POSIX behavior or POSIX file modes. Repeat the root-suite command above on Linux; Windows results do not validate those paths.
- Live validation `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 1 --task-timeout-seconds 60` for each registered benchmark cannot run on this review host: neither OpenClaw nor Docker is available on PATH, and the host is Windows. Confirm actual deadline enforcement, producer cleanup, and final KB flush on the target Linux runtime.

## Benchmark validation on the target Linux host (2026-09-18)

An earlier validation attempt could not run `tests/test_benchmark_timeout.py` because it was absent. The unified-budget change now supplies and validates that file (see above).

The following requested default-roster validation commands cannot select the
requested two tasks, even in `--dry-run` mode, because the tracked smoke roster
contains only one entry.  Each exits 1 with `requested 2 tasks, only 1
available`; use `--sample 1` for the current smoke roster, or add a second
tracked valid smoke task before requiring this acceptance command.

- `python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified --sample 2 --parallelism 2 --task-timeout-seconds 1200 --dry-run`
- `python3 scripts/clawtune.py benchmark --benchmark bfcl --sample 2 --parallelism 2 --task-timeout-seconds 1200 --dry-run`
- `python3 scripts/clawtune.py benchmark --benchmark terminal-bench --sample 2 --parallelism 2 --task-timeout-seconds 1200 --dry-run`

Before the timeout-separation change, on `weitianc@193.124.7.2` (OpenClaw
2026.7.1), the otherwise selectable SWE-Rebench and Deep Research Bench live
commands failed:

- `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 2 --parallelism 2 --task-timeout-seconds 1200`
- `python3 scripts/clawtune.py benchmark --benchmark deep-research-bench --sample 2 --parallelism 2 --task-timeout-seconds 1200`

They failed before an agent or tool call because the generated run-local
OpenClaw configuration set `agents.defaults.timeoutSeconds: 0`, which this
OpenClaw version rejects (`must be greater than 0`).  The local fix removes both
benchmark-written OpenClaw timeout fields and retains only the 1,200-second
whole-task supervisor deadline. Deploy it to the target host and rerun these
commands before treating trace-quality acceptance as complete.

After deploying that fix, the SWE-Rebench command reached its LLM span without
the configuration error, but both selected tasks received upstream HTTP 402
(provider balance/credit exhausted) before a tool call. Live tool-trace and
metric-quality acceptance remains blocked until a funded provider credential is
available; this is not a timeout result.

- Timeout/cancellation audit: `PYTHONPATH=services/sidecar/src python3 -m pytest tests/test_timeout_finalization.py tests/test_benchmark_cancellation.py tests/test_benchmark_shutdown.py tests/test_benchmark_runtime_fixes.py tests/test_benchmark_terminal_exec.py services/sidecar/tests/test_runtime_abort.py -q -rs` still requires Linux for the eight subreaper/procfs/POSIX cases skipped on Windows. Live concurrent timeout acceptance additionally requires Linux, Docker, OpenClaw and provider credentials; exercise `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 --task-timeout-seconds 60` for each adapter, plus interruption during setup and tool execution, and verify no surviving producers and an acknowledged final KB flush.
  The targeted WSL variant using `tests/test_benchmark_shutdown.py tests/test_benchmark_terminal_exec.py tests/test_timeout_finalization.py tests/test_benchmark_cancellation.py services/sidecar/tests/test_runtime_abort.py` cannot run in the installed Ubuntu environment because `/usr/bin/python3` has no `pytest` module.

This file records checks that still require a suitable environment. Usage and configuration belong in the [installation guide](getting-started.md) and [benchmark guide](benchmarks.md).

The 2026-09-16 review reference for main commit `5e68953` is not included in this checkout. The eBPF-default redesign is implemented in the working tree; Linux kernel acceptance remains outstanding.

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

- Cross-process mixed-benchmark sampling acceptance remains pending on Linux with Docker, OpenClaw, BCC/perf privileges and provider credentials: run `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 6 --parallelism 2` and `python3 scripts/clawtune.py benchmark --benchmark terminal-bench --sample 6 --parallelism 2` concurrently, with datasets containing at least six tasks each. Check per-execution ownership, event loss, actual sample gaps and four-event PMU running ratios against independent per-execution counters. Windows unit tests do not establish live accuracy.
- Linux-only checks for this sampling review: `PYTHONPATH=services/sidecar/src python3 -m pytest tests/test_benchmark_terminal_exec.py -q -rs` must exercise the three procfs/POSIX cases skipped on Windows; `python3 tools/check_ebpf.py` and `python3 tools/validate_pmu.py --concurrency 4 --require-reliable` require the target Linux BCC/perf environment.
- RSS quality follow-up: both `monitoring/ebpf_tool.py` and `tool_resource/clause_bridge.py` need coverage checks for an address space with only one sample or missing lifetime edges. Dense peer samples can currently leave the aggregate marked eligible/ok despite incomplete coverage of that address space. Validate sleeping memory holders and compare against an independent RSS reference; keep cgroup memory-charge labels separate.


## Pending Linux validation: independent predictions and native tool windows

- Run `PYTHONPATH=services/sidecar/src python3 -m pytest services/sidecar/tests tests -q -rs` and `python3 tools/check_ebpf.py` on Linux. BCC/perf attachment and POSIX-only cases cannot run on the Windows development host.
- Run `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 6 --parallelism 2` with the updated plugin and sidecar. This requires Linux, Docker, OpenClaw and provider access. Check independent `prediction.tool/trie/lattice`, environment-memory eligibility, and read/edit PID resolution retaining the pre-action window. No new-run coverage or prediction accuracy improvement has yet been measured.

- Targeted `python -m ruff check services/sidecar/src/clawtune_sidecar/predictors/call_load.py services/sidecar/src/clawtune_sidecar/monitoring/ebpf_tool.py services/sidecar/src/clawtune_sidecar/monitoring/tool_runtime.py services/sidecar/tests/test_call_load.py services/sidecar/tests/test_ebpf_tool_monitor.py` could not run: Ruff is not installed in the local Python environment.

- Cold-start audit acceptance: `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 4 --parallelism 2` requires the Linux/Docker/OpenClaw deployment and cannot run on this Windows host. Confirm that valid unavailable model outputs do not generate prediction audit issues and per-model coverage remains recorded.

- Pristine task-image dependency check is pending because SSH to `kunpeng` timed out: run `docker run --rm --pull never --network none --read-only --platform linux/amd64 --entrypoint /opt/miniconda3/envs/testbed/bin/python swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64 -B -c 'import sys,pydantic; print(sys.executable); print(sys.version); print(pydantic.__version__)'` remotely to compare the original Pydantic version with the trace.

## Pending validation: workload labels and prediction coverage

- `ssh -o BatchMode=yes -o ConnectTimeout=10 kunpeng "pwd"` timed out. Deployment and `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 4 --parallelism 2` remain blocked by remote connectivity. Deploy this working tree, including the existing prediction-audit changes, and use a fresh run-local KB. Measure per-model/per-metric training acceptance, prediction coverage and paired errors separately; exclude consumer-scope mismatches from like-for-like error claims.
- `PYTHONPATH=services/sidecar/src python3 -m pytest services/sidecar/tests tests -q -rs` still requires Linux for the POSIX cases and `test_native_parser_resolves_literal_head`. The direct `parse_command_clauses` probe could not build the native mvdan adapter on Windows (missing bundled binary/POSIX builder); the installed WSL environment has no Go executable. Validate literal `$v`/`${v}` assignment parsing and runtime executable matching on Linux.
- `python3 tools/check_ebpf.py` and the fresh benchmark above must validate actual collector boundaries, execution-window admission and recent environment baselines. Unit tests and old traces do not establish new-run coverage or measurement accuracy. Confirm first-call missing baselines and overlapping environments remain unavailable.
