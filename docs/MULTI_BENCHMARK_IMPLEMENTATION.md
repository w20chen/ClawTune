# Three paths and peer benchmark adapters

Status: current implementation reference. CLI syntax is summarized here;
`python3 scripts/clawtune.py benchmark --help` remains authoritative.

2026-09-11: supersedes the SWE-only and DRB-removal decisions in DEMO_SYSTEM_DESIGN.md.

```
OpenClaw daily use -> plugin -> sidecar -> user KB
benchmark CLI -> common run lifecycle -> peer adapter -> OpenClaw + sidecar -> run KB
offline CLI -> fixed trace loader -> grouped task split -> train seed -> frozen test
```

`benchmarks/` owns registry, normalized tasks, run selection, progress and KB
ownership. Its peer adapters are swe-rebench, deep-research-bench,
swe-bench-verified, bfcl and terminal-bench. Repository tasks share a runtime;
research uses web tools; BFCL preserves its functions and multi-turn state;
Terminal Bench preserves task-owned Docker environments. Existing SWE/DRB host
utilities are implementation reuse, not parent benchmark types.

`clawtune_kb` is a small sidecar-independent storage package. Immutable seeds
live under `seeds/`; user state under the invoking user's state directory;
simulation and offline outputs under `.runtime/`. No automatic cross-run or
cross-dataset merge. Within one run tasks share one predictor and one
asynchronous writer. `parallelism` bounds tasks in flight; `1` is serial.

Concurrent tasks do not wait for one another. Accepted observations become
visible under the predictor lock and enqueue a persistence notification. The
single writer coalesces notifications without losing updates. Per-task drain
waits for only that runtime's executions/finalizers, while one final durability
barrier commits the complete queue before the sidecar stops.

Offline identity is (benchmark, task ID), never filename alone. Repository
datasets split within repo. Non-repository datasets split within an explicit
category, or within their dataset when categories are absent; reports name
this grouping honestly. Entire multi-turn tasks and all attempts stay together.
Call latency is usable across all suites; clause targets require actual clause
telemetry, and missing resource labels are unavailable, not zeros.

Implementation order: storage and CLI contracts; peer task adapters and common
runner; BFCL/terminal tool execution adapters; offline pipeline; focused tests,
documentation and compatibility entrypoint cleanup. The external
agent-test-bench checkout is a read-only implementation reference, not a writable
runtime workspace and not a replacement for ClawTune's plugin/sidecar.

## Public paths and modules

| Path | Entry | Ownership | Implementation |
| --- | --- | --- | --- |
| Daily | OpenClaw gateway / agent | invoking user | plugin + sidecar + `clawtune_kb` |
| Online simulation | `scripts/clawtune.py benchmark` | one dataset / one run | `benchmarks/{cli,adapters,runner,runtime,backends,tool_bridge}.py` |
| Offline evaluation | `scripts/clawtune.py offline` | one dataset / one experiment | `offline/runner.py` + trace loaders + shared prediction core |

`configs/benchmark.example.yaml` is the common model/runtime template.
`contracts/` defines seed, state, split, run, report, and tool-bridge formats.
The sidecar wheel copies these canonical contracts and the bundled seed at build
time. There is no independently edited schema copy.

The old SWE and research host helpers remain because they contain the existing
sandbox, launcher, eBPF, and trace finalization implementation. They are internal
reuse behind the common runner, not two separate public workflows. The public
replay path and unused wrapper implementations were removed. Historical export
scripts and old evaluator modules remain for comparing existing research reports;
they do not select the default KB and are not additional supported demo paths.

## Input contracts and native behavior

Task files accept a JSON array, JSONL objects, or an object with a `tasks`,
`instances`, or `data` array. `--dry-run` validates selection and seed without
starting Docker, OpenClaw, a backend, or the LLM.

| Adapter | Input | Execution | Limits |
| --- | --- | --- | --- |
| swe-rebench | `instance_id`, `repo`, `problem_statement`, dataset `docker_image`; optional `base_commit` | Existing native OpenClaw repository sandbox and launcher | Requires dataset image; no guessed fallback image |
| swe-bench-verified | Same SWE fields; image optional | Same repository executor, independent adapter and KB | Default official x86_64 task image naming; compatible Docker/QEMU required |
| deep-research-bench | `id`, `prompt`/`question`, optional `topic`/`domain`, `article` | Native OpenClaw web tools and basic sandbox | Search provider credentials needed; reference article is record-only |
| bfcl | Native processed `question` turns, `function`, `involved_classes`, `initial_config`, `id`; or native `--category` loader | BFCL native class instances exposed through OpenClaw plugin tools | Executable stateful categories only; AST-only evaluation is not simulated |
| terminal-bench | Directory of native tasks, or rows with `task_path`/`task_source_path` | Copied task Compose project and its `client` container | Requires Compose task format; shell calls are not a persistent interactive terminal |

BFCL native dependencies must be installed into the same `.venv` as the runner.
`BFCL_REPO_PATH` points to the Gorilla repository containing
`berkeley-function-call-leaderboard/bfcl_eval`. Memory state writes into the run;
tool instances and the OpenClaw session persist across all turns of one task.
Each subsequent task starts new backend state. No function-call text is `eval`ed.

Terminal tasks are copied before Compose is resolved. Bind mounts and build
contexts must resolve within the copied run. External host binds are rejected.
Named project/container identities are unique per task, and cleanup targets only
that project. Tools operate in the real task container; this path currently
captures call latency through hooks, not attributed clause CPU/RSS from that
container. No synthetic resource labels are supplied. This MVP does not invoke
the official Terminal harness grader or provide a persistent TTY.

All five adapters are user simulations, not official leaderboard evaluators.
Reports therefore expose `official_score: null`. They can show valid tool traces
and per-task learning independently from whether the agent solved the task.

## KB and evaluation rules

- Daily: `~/.local/state/clawtune/kb`, or `$CLAWTUNE_STATE_DIR/kb`.
- Online: `.runtime/benchmarks/<benchmark>/<run>/kb`, initialized from
  `seeds/demo-v1` or `--seed`; never merged back into daily state.
- Offline: `.runtime/offline/<experiment>/<benchmark>/seed` for mixed datasets;
  a single dataset writes its seed directly under the experiment.
- A seed contains exactly clause-resource, runtime-tool-resource and lattice-time
  snapshots with SHA-256 provenance. The historical bundled seed contains the
  paired clause/lattice training snapshot and an explicitly empty runtime layer.
- Managed state has one writer. Generations commit all three snapshots atomically
  through `CURRENT`; current and preceding snapshots are retained. This provides
  process-crash recovery, not a promise of retaining uncommitted in-memory events.
- Online task namespaces and offline namespaces both use `<benchmark>:<group>`.
  Separate runs/datasets never accidentally share global fallback nodes.
- Offline split hashes `(seed, benchmark, group, task)`; a group of N tasks gets
  `max(1, floor(0.8*N))` training tasks. SWE groups by repository; other datasets
  use explicit category or dataset. All attempts remain on one side of the split.
- `trace.jsonl` is canonical within an attempt when sibling replay logs exist;
  aggregate multi-task simulation files are excluded and recorded in `split.json`.
  Trace v6 needs identity metadata or adjacent `dataset-task.json`, which the new
  online runner emits. Filenames alone are not a task identity contract.
- Test queries use only pre-execution tool/command features and the same
  `predict_call_load` implementation as online. Labels use the runtime's target
  eligibility gates. Historical v5 shared cgroup CPU/RSS are not treated as
  authoritative per-call resource labels.
- Reports include coverage, MAE, WAPE, task-macro MAE, P90 coverage and a paired
  train-only tool-median baseline. These are prediction metrics, not solve rates.
- Pipeline-dependent consumers are excluded by the shared modeling rule;
  standalone `grep`/`cat` and independent file operands remain modelable.

## Validation boundary (2026-09-11)

The Windows workspace can validate adapters, state recovery, schema handling,
offline evaluation, bridge requests, packaging and plugin behavior. Linux live
OpenClaw/Docker/eBPF acceptance and native BFCL/Terminal integrations still need
the target Linux environment. Mocked backend tests are not claimed as live runs.
Exact commands and results are recorded in `CURRENT_PLAN.md`.

The new offline runner was exercised against the read-only local 277-task SWE
trace collection: 239 train / 38 test, 1,977 eligible test tool calls, zero test
updates. Duration MAE was 1,643.62 ms versus 1,687.76 ms for the paired baseline;
WAPE 0.9705, P90 coverage 0.7699. This modest improvement is not evidence of strong
resource-prediction accuracy. The data supplied only eligible call-duration
labels under the conservative v5 attribution gate. No external dataset changed.
