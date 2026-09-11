# Current Plan

This file records current behavior, known limitations, and validation that
cannot run in this workspace. User instructions live in the dedicated guides;
implementation history lives in Git.

## CI regression fixes (2026-09-11)

The reported root-suite failures exposed a dependency on a deleted historical
Git blob, an incomplete setuptools test double after adding the sdist hook,
and a packaging subprocess whose captured stderr was hidden on failure.
The development dependencies now explicitly include setuptools and wheel:
PEP 517's isolated build requirements do not install these into the test
interpreter. A new Python 3.12 venv reproduced `ModuleNotFoundError: setuptools`
before installing the development extra. The original CI packaging stderr was
not included in the supplied log, so its precise failure remains unconfirmed.

The recipe regression now builds twice from explicit synthetic source data,
checks selected median/quantile durations and identity removal, and compares
all output bytes. Missing historical input has an actionable error and its own
regression. The shipped seed is unchanged. A separate local reconstruction
from the original historical object matched all four shipped files byte for
byte; the command is documented in [bootstrap reproduction](bootstrap-kb.md).

- `python -m pytest tests/test_bootstrap_seed.py tests/test_release_packaging.py
  tests/test_clawtune_cli.py -q --basetemp .pytest-tmp-ci-fixes`: 41 passed.
- `python -m pytest tests -q --basetemp .pytest-tmp-root`: 385 passed,
  2 Windows platform skips.
- `python -m pytest` in `services/sidecar`: 409 passed, 2 platform skips.
  Existing FastAPI deprecation and Windows pytest-cache permission warnings
  do not fail the suite.
- `python -m venv .runtime/ci-clean-venv`, followed by that environment's
  `python -m pip install -e services/sidecar[dev] jsonschema`: passed using
  the same dependency installation command as CI, without system packages.
  Its `python -m pytest tests -q --basetemp .pytest-tmp-clean-ci` also passed:
  385 passed, 2 Windows platform skips, including source-release packaging.
- `python tools/validate_docs.py`: 28 Markdown files, 87 local links, no errors.
  `git diff --check`: passed.
- The Ubuntu GitHub Actions job cannot be executed on this Windows host;
  a remote CI rerun is still required. No Linux telemetry acceptance is claimed.

## Small release bootstrap (2026-09-11)

Follow-up release audit: the original source distribution omitted both seed
and public contracts, despite direct checkout-to-wheel builds passing. The
sdist hook now copies canonical runtime data into the source release before
archiving; both packaging paths use the same bundle helper.

- `python setup.py sdist --dist-dir ../../.runtime/bootstrap-sdist` in
  `services/sidecar`: passed after the fix. Initial archive inspection found
  zero seed/schema files and exposed the packaging omission.
- `python -m pip wheel .runtime/bootstrap-sdist/clawtune_sidecar-0.1.0.tar.gz
  --no-deps --no-build-isolation --wheel-dir .runtime/bootstrap-sdist-wheel`:
  passed without access to the parent source checkout during the wheel build.
- Independent extracted-wheel check in a system temporary directory: all four
  seed files match the checkout, bundled schemas validate, actual sidecar state
  initialization selects bootstrap-v1, and restart preserves that state even
  with a subsequently invalid seed override. Collector execution was disabled;
  this is initialization validation, not Linux telemetry acceptance.
- `python -m pytest tests/test_release_packaging.py tests/test_bootstrap_seed.py
  -q -p no:cacheprovider`: 8 passed, including an isolated source-release
  packaging regression.
- `python tools/validate_docs.py` and `git diff --check`: passed.

Daily startup, the unified benchmark CLI, retained host helpers and sidecar
wheel builds now default to `seeds/bootstrap-v1`. It contains 40 real,
executable-only clause observations (eight per cat/find/grep/ls/which), with
all repo names, task identities, paths and original arguments removed.
TrieKB has public priors only; LatticeKB has no repo feature; ToolKB is empty.
Six observations retain CPU time/average and sampled RSS. Short or unreliable
resource labels are withheld; no peak CPU or PMU evidence is fabricated.
The bundle totals 16,226 bytes. This is a small uncalibrated SWE-derived prior,
not a claim of measured cross-benchmark or cross-hardware accuracy.

Removed the four files in the retired `seeds/demo-v1` and four old KB/manifest
files under `traces/tool-resource`. No raw/external trace data, existing daily
state or prior run-owned KB was deleted. Historical export/evaluation tools
now default to `.runtime/lattice-export` to keep generated KBs out of source.
The cold-start split manifest is retained as experiment metadata, not a KB.

`scripts/build_bootstrap_seed.py` reconstructs the bundle from an immutable
historical Git object, or an explicit original snapshot. Tests confirm exact
reproduction, source identity removal, small size, resource gates, identical
predictions across unrelated repo names, no canonical prediction for an
unknown executable, and ability to learn without modifying the seed. See
[bootstrap contents and reproduction](bootstrap-kb.md).

Validation for this change:

- `python -m pytest tests -q -p no:cacheprovider`: 383 passed, 2 platform skips.
- `python -m pytest -q -p no:cacheprovider` in `services/sidecar`:
  409 passed, 2 platform skips.
- `python tools/validate_contracts.py`: 14 examples passed.
- `python tools/validate_docs.py`: 28 project Markdown files, 86 local
  links/anchors passed, including new untracked guides.
- `python scripts/clawtune.py benchmark --sample 2 --parallelism 2 --dry-run`:
  selected the new release seed successfully.
- `python -m pip wheel ./services/sidecar --no-deps --no-build-isolation
  --wheel-dir .runtime/bootstrap-wheel`: passed. Archive inspection found
  exactly four seed files, all in bootstrap-v1 and byte-identical to source.
  Importing the extracted wheel outside the repository source validated its
  bundled seed and public schemas successfully.
- `git diff --check`: passed.

Failed or unavailable validation:

- The first sidecar suite returned 405 passed, 2 skipped, 4 failures because
  old tests read the now-removed large/synthetic KB snapshots. Tests now verify
  empty initial ToolKB, isolated online learning and the new public clause
  priors. The complete rerun passed as recorded above.
- Linux OpenClaw/Docker/eBPF live startup and measured online accuracy cannot
  run in this Windows workspace. Unit and package checks do not establish
  hardware accuracy or calibration; use the Linux acceptance commands below.

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
| `python tools/validate_docs.py` | 27 tracked Markdown files, 81 local inline links/anchors, no errors |
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

## Documentation naming update

All maintained guides and historical reports use ToolKB, TrieKB and LatticeKB.
Legacy Python identifiers appear only in the architecture naming map; existing
filenames, schema keys and quoted console labels retain their actual values.
A scan of all 27 tracked Markdown files confirms the old class names occur only
in that map. `python tools/validate_docs.py` passes all 81 local links/anchors;
`git diff --check` passes. This update changes documentation only. An additional check with
`git -c core.autocrlf=false diff --check` initially reported CRLF characters as
trailing whitespace in edited guides. Their line endings were normalized and
both whitespace checks rerun successfully.

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
