# Current Plan

This file records current behavior, known limitations, and validation that
cannot run in this workspace. User instructions live in the dedicated guides;
implementation history lives in Git.

## Current state

ClawTune supports three workflows:

- daily OpenClaw use with a persistent user KB;
- online benchmark simulation with a run-owned, learning KB;
- fixed-trace offline training and frozen task-held-out evaluation.

The public benchmark registry contains SWE-Rebench, Deep Research Bench,
SWE-bench Verified, BFCL, and Terminal Bench. `--parallelism N` sets the
maximum number of tasks in flight; `1` is serial. The runner uses one shared
sidecar and KB per invocation.

Benchmark tasks do not wait at a batch barrier. Each task owns its config copy,
runtime identity, workspace, trace directory, and bridged-tool manifest.
Accepted observations update the in-memory predictor under its KB lock and
enqueue persistence on one background writer. That writer coalesces concurrent
notifications and commits all three KB snapshots atomically through `CURRENT`.
Per-task drain waits for only that runtime's active executions and finalizers;
it deliberately does not force a global KB flush. After all task producers
finish, the runner rechecks every actual task runtime, then performs one
durability barrier, records
`kb_final_generation`, and only then stops the sidecar.

`run.json` is written only by the coordinating thread. It records source order,
configured parallelism, completed results, and the currently in-flight task
IDs. A run with interrupted in-flight tasks or an incomplete final KB barrier
is rejected on resume because replaying it could duplicate or lose learning.
Per-task generation intervals observe the shared KB and may include peer
updates; they are not causal attribution.

Coordinator interruption signals cooperative cancellation before joining
workers. Agent waits poll the signal even with timeouts disabled, terminate
the agent process group, and run task cleanup. Host setup commands for
onboarding, plugin installation/build, telemetry preflight, image pulls and
repository export also poll cancellation. Other setup operations check it at
phase boundaries and retain their existing command timeouts. Finished worker
results are retained on interruption; unsafe executor failures stop further
dispatch and leave the run non-resumable. Cleanup is allowed to finish before
the sidecar is stopped.

JSON Schemas under `contracts/` are the public protocol source of truth. Trace
JSONL uses format version 6 and API lifecycle events use `clawtune.v1` or
`clawtune.v2` as defined by their schemas. Placement remains advisory.

## Known limitations

- Live benchmark execution requires Linux, Docker, OpenClaw, cgroup v2,
  matching kernel headers, BCC/eBPF privileges, and provider credentials.
- Task cancellation cannot safely resume an in-flight task because it may have
  already contributed observations.
- Parallel tasks intentionally see accepted peer observations according to
  real completion order. The run is reproducible in task selection and state
  integrity, not in the exact interleaving of online learning.
- Deep Research Bench depends on a usable OpenClaw web-search provider and key.
- CPU for short native sandbox tools can be PID-correlated while still coming
  from a shared sandbox cgroup; traces report this attribution boundary.
- PMU support is best effort. Permissions, unavailable counters, multiplexing,
  and FD budgets reduce coverage but do not fail tool execution.
- Legacy traces lack some causal timestamps and resource anchors. Missing
  labels remain unavailable rather than being synthesized.

## Latest commit review (2026-09-11)

Reviewed `f6bb4b6` (`update (not finished)`) from a clean working tree.
The commit correctly consolidated operating guides and added the Verified
historical directory alias, Terminal manifest-relative paths/Dockerfile support,
BFCL unsupported-category checks, task-local research keys and exported LLM keys.
Those changes are retained. The sibling reference checkout's source confirms
its historical dataset directory names, but this Windows checkout contains no
`data/` directory; default discovery here uses the bundled SWE/research lists.
No external source or trace dataset was modified.

Follow-up fixes:

- Research cleanup failures now propagate to the coordinator after attempting
  trace preservation and remaining cleanup. Failed drain no longer skips trace
  copying. This retains unsafe task ownership instead of allowing new dispatch.
- BFCL bytecode caches are redirected alongside its result/lock directories.
  The installation example builds a local copy to avoid pip metadata writes in
  a read-only reference checkout.
- Terminal retains explicit manifest IDs, diagnoses Harbor parent directories,
  rejects invalid native timeout values and applies the native agent budget
  across the conversation after setup. Shell calls also retain a 300-second cap.
- Corrected the obsolete ARM research exception and documented environment/YAML
  precedence, native arm64 configuration, timeout boundaries and plugin command
  working directories. Removed two superseded redirect-only plan files.
- Root README, workflow guides and technical references now have a reproducible
  local-link/heading check. Historical experiment reports remain explicitly
  historical; they are not instructions for the unified runner.

Upstream interface checks used the BFCL
[output/cache configuration](https://github.com/ShishirPatil/gorilla/blob/main/berkeley-function-call-leaderboard/bfcl_eval/constants/eval_config.py),
[native loader](https://github.com/ShishirPatil/gorilla/blob/main/berkeley-function-call-leaderboard/bfcl_eval/utils.py),
and Terminal v1
[task model](https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/handlers/trial_handler.py)
and [Compose environment](https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/terminal/docker_compose_manager.py).
These dependencies are not vendored or revision-pinned by ClawTune; record the
installed revision with experiments. Static interface review is not a live run.

## Validation (2026-09-11, current working tree)

| Command / check | Result |
| --- | --- |
| `python -m pytest tests -q -p no:cacheprovider` | 376 passed, 2 platform skips |
| `python -m pytest -q -p no:cacheprovider` in `services/sidecar` | 409 passed, 2 platform skips |
| `npm.cmd test` in `packages/clawtune-plugin` | Build and 99 tests passed |
| `npm.cmd run typecheck` in `packages/clawtune-plugin` | Passed |
| `python tools/validate_contracts.py` | 14 examples passed |
| `python tools/validate_docs.py` | 27 tracked Markdown files, 78 local inline links/anchors, no errors |
| `python scripts/clawtune.py benchmark --sample 2 --parallelism 2 --dry-run` | Bundled SWE source passed |
| `python scripts/clawtune.py benchmark --benchmark deep-research-bench --sample 1 --dry-run` | Bundled research source passed |
| `benchmark --benchmark <name> --dataset <fixture.json> --sample 1 --dry-run` | Verified, processed BFCL and manifest-relative Terminal fixtures passed |
| `--help` for setup/doctor/check/benchmark/offline/kb, both discovery modules and trace inspection | 9 commands passed |
| `git diff --check` | Passed |

Fixture dry-runs used temporary files under ClawTune `.runtime/`, then removed
only those fixtures. BFCL backend state/turns and Terminal Compose setup are
mock-backed tests; dry-run success does not certify a native backend. Existing
mixed-benchmark offline tests exercise all five identities and frozen splitting.
The Python environment emits an existing Requests dependency warning; sidecar
tests also emit FastAPI lifecycle deprecation warnings. Neither failed a suite.

Validation that failed or could not run:

- The first focused adapter/research regression command returned 2 failures:
  the cache test assumed Python would materialize a cache file under a long
  Windows temporary path. It now verifies a real source import, redirected
  cache write destination and absence of source-side cache writes without
  requiring optional bytecode materialization. The focused rerun passed (53 tests); the later
  full root suite includes the added Terminal timeout regressions above.
- `python3 scripts/clawtune.py setup`, `check`, live benchmark commands and
  `python3 tools/validate_pmu.py --require-reliable ...` cannot be accepted on
  this Windows host: Linux BCC/eBPF, cgroup v2 and the supported privileged
  Docker/OpenClaw/provider runtime are unavailable. No model call, live backend
  or hardware PMU measurement was performed.
- Native BFCL category loading and a real Terminal task checkout were not
  executed here; the external data directory is absent and the required native
  packages/runtime are not provisioned. Processed fixtures do not replace that
  acceptance. The first attempted upstream Terminal default-Compose URL was
  unavailable; its actual Compose-manager and task-model sources were checked.
- Full upstream dataset downloads and historical experiment reruns were not
  performed. They need their specified external inputs and are not established
  by the checked-in smoke lists.

Older validation attempts and resolved failures are preserved in Git history,
not repeated as current failures here. Run root and sidecar pytest suites in
their separate package contexts; root-wide recursive discovery is unsupported.

## Linux acceptance still required

On each supported deployment architecture, after setting model credentials and
preparing the documented sources:

```bash
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py check
python3 scripts/clawtune.py benchmark --sample 4 --parallelism 2
TAVILY_API_KEY="<key>" python3 scripts/clawtune.py benchmark \
  --benchmark deep-research-bench --sample 4 --parallelism 2
python3 scripts/clawtune.py benchmark --benchmark swe-bench-verified \
  --dataset /data/verified.jsonl --sample 2 --parallelism 2
python3 scripts/clawtune.py benchmark --benchmark bfcl \
  --category multi_turn_base --sample 2 --parallelism 2
python3 scripts/clawtune.py benchmark --benchmark terminal-bench \
  --dataset /data/terminal-bench/original-tasks --sample 2 --parallelism 2
python3 tools/validate_pmu.py --require-reliable \
  --concurrency 8 --max-active 8 --high-concurrency 64 \
  --benchmark-count 40 --output traces/pmu-validation.json
```

Verify bounded concurrency, distinct runtimes/workspaces, strict repository
cgroup/eBPF gates, one result per task, no `active_tasks` at successful completion,
`kb_flush_complete: true`, and reopening `kb/` at `kb_final_generation`.
Exercise timeout/Ctrl+C and container cleanup, BFCL state across turns, research
provider selection, Terminal native timeout and source immutability. An unsafe
cleanup must stop further dispatch and reject resume. Hook-only BFCL/Terminal
traces must not claim attributed CPU/RSS/PMU observations or official scores.
