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
finish, the runner performs one durability barrier, records
`kb_final_generation`, and only then stops the sidecar.

`run.json` is written only by the coordinating thread. It records source order,
configured parallelism, completed results, and the currently in-flight task
IDs. A run with interrupted in-flight tasks or an incomplete final KB barrier
is rejected on resume because replaying it could duplicate or lose learning.
Per-task generation intervals observe the shared KB and may include peer
updates; they are not causal attribution.

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

## Validation (2026-09-11)

Completed in this Windows workspace:

- `python -m pytest tests -q -p no:cacheprovider ...`:
  **337 passed, 2 platform skips**.
- `python -m pytest` from `services/sidecar`:
  **408 passed, 2 platform skips**.
- Focused benchmark/sidecar/predictor concurrency suite:
  **167 passed**.
- `npm.cmd test` in `packages/clawtune-plugin`:
  **99 passed**; `npm.cmd run typecheck` passed.
- `python tools/validate_contracts.py`:
  **14 examples passed**, including the concurrent benchmark-run manifest.
- `python scripts/clawtune.py benchmark --sample 2 --parallelism 2 --dry-run`:
  passed and selected two tasks.
- The focused tests prove overlapping task execution, task-local config
  isolation, no process-global BFCL/Terminal manifest race, runtime drain
  without a persistence barrier, concurrent completion coalescing without lost
  updates, one final durability barrier, and a final committed generation.
- Source-only `py_compile`/`compileall` and `git diff --check` passed after
  excluding inaccessible stale pytest directories from recursive discovery.

Validation commands that could not run or could not complete as issued:

- `python3 scripts/clawtune.py setup`, `check`, and a live benchmark cannot run
  on Windows because Linux BCC/eBPF, cgroup v2, Docker host behavior, OpenClaw,
  and model credentials are not available here.
- `python -m pytest -q` from the repository root also collected the separately
  packaged sidecar suite without `services/sidecar/src` on `PYTHONPATH`, so it
  stopped during import collection. The root and sidecar suites are validated
  independently in their package contexts above.
- An attempted explicit root-suite command named `swe_rebench/tests`,
  `deep_research_bench/tests`, and `legacy_eval/tests`, but those directories
  do not exist; all non-sidecar tests are under the root `tests/` directory.
- `python -m ruff check ...` could not run because Ruff is not installed in
  this Windows environment. Python compilation and both pytest suites passed.
- The first recursive PowerShell Markdown-link scan used `Get-ChildItem
  -Recurse` and hit access-denied stale pytest directories under
  `services/sidecar/.pytest_cache` and `swe_rebench/.pytest-*-tmp`. The final
  link validation uses only paths returned by `rg --files`.
- A later `rg --files` link-check invocation mishandled root-level Markdown
  paths and emitted `Join-Path` errors. Its success line is disregarded; the
  corrected invocation uses `.` when a file has no parent directory.
- A broad `python -m compileall ... swe_rebench ...` could not enumerate the
  same inaccessible stale pytest directories. Source-only compilation is run
  separately.
- The first combined compile/help/contracts/diff command returned nonzero
  because `git diff --check` found two extra EOF blank lines; those lines were
  removed and the check was rerun.

## Linux acceptance still required

On each supported deployment architecture:

```bash
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py check
python3 scripts/clawtune.py benchmark --sample 4 --parallelism 2
TAVILY_API_KEY="<key>" python3 scripts/clawtune.py benchmark \
  --benchmark deep-research-bench --sample 4 --parallelism 2
```

Verify that no more than two tasks are in flight, runtime identities and
workspaces remain distinct, repository tasks pass strict cgroup/eBPF gates,
all results are recorded once, `active_tasks` is absent at completion,
`kb_flush_complete` is true, and reopening `kb/` exposes
`kb_final_generation` with observations from every eligible completion.
