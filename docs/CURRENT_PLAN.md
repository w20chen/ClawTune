# Current Plan

Current objective: provide one reliable, eBPF-required user journey from host
setup to OpenClaw and SWE-Rebench execution, prioritizing Kunpeng/openEuler
while retaining native x86_64 Linux support.

## Supported user commands

The user-facing path is intentionally limited to:

```bash
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py doctor
python3 scripts/clawtune.py agent --local --agent main --model "vllm/<model>" --message "..."
python3 scripts/clawtune.py sidecar
python3 scripts/clawtune.py benchmark --sample 1
```

The wrapper selects a system Python with `bcc`/`bpfcc`, owns the single `.venv`
path, supplies kernel/sudo environment, installs distro packages through `dnf`
or `apt`, builds/configures the plugin, and handles amd64 QEMU on Kunpeng.
Historical internal collector names below are retained as engineering records,
not as user installation instructions.

The public configuration defaults `docker.cgroup_required: true`. A false
value is troubleshooting-only and its output is not accepted as complete
per-tool telemetry.

## Shared-sidecar Stage-2 artifact collection (2026-08-04)

A `host-openclaw-sandbox` batch run can fail every task with
`required Stage-2 telemetry produced no exec artifacts` even though the agent
succeeded and the trace records a complete Stage-2 lifecycle/envelope for every
launcher command. Root cause: with the shared batch sidecar, the sidecar writes
one per-execution clause telemetry artifact (`<execution_id>.json`) into the
shared KB directory, and neither `_collect_runtime_traces` (which snapshots only
`{runtime_id}__*.jsonl`) nor `_publish_tool_resource_kb` (which is intentionally
skipped while the shared sidecar owns the KB) moves those artifacts into the
per-task trace directory. The per-task `trace_dir/tool-resource/` therefore
stays empty and the required-telemetry gate sees `artifact_count == 0`.

Fix (in `swe_rebench`, no `tool_resource` change): `run_host_sandbox_task` now
calls `_collect_runtime_stage2_artifacts(shared_kb_dir, trace_dir)` in the same
shared-sidecar teardown that snapshots runtime traces. It scans the collected
per-runtime trace JSONL, resolves every `execution.tool_resource.artifact_path`
against the shared KB directory, validates each payload is a complete clause
artifact (`mode == "clause"`), and copies it into
`trace_dir/tool-resource/` — the same layout a per-task sidecar produces. Each
task trace stays self-contained and independently auditable, the
required-telemetry gate counts the on-disk artifacts, and the existing
`_inspect_tool_resource_artifacts` fallback (shared-KB snapshot names are
skipped) needs no change.

Validation completed in the Windows development workspace:

- `python -m pytest tests -q --basetemp .pytest-tmp-root-full3` (with
  `PYTHONPATH=services/scheduler/src`): `178 passed, 2 skipped`.
- Focused shared-sidecar artifact suite:
  `python -m pytest tests/test_swe_rebench_selection.py -q -k "stage2_artifact or shared_sidecar or collect_runtime"`:
  `4 passed`.
- `git diff --check`: passed.

Validation commands that cannot run in this Windows workspace:

- The live reproduction
  `python3 scripts/clawtune.py benchmark --sample 1` (and a parallel
  `--sample N --parallelism N` run) requires the Linux host, Docker, cgroup v2,
  and BCC/eBPF. It is the acceptance test for this fix: a task whose agent
  produces a patch must no longer be rejected by the Stage-2 gate.

## Serial benchmark knowledge sharing (2026-08-02)

`benchmark --sample N` now preserves task-source order and executes the
selected tasks serially against one run-scoped aggregate KB generation. The
runner copies the current runtime/clause snapshot pair into each task and,
only after its sidecar or container exit is confirmed, transactionally
publishes the valid pair for the next task. A partial two-file publish rolls
back and aborts the remaining batch instead of exposing a mixed generation.

The aggregate keeps the scheduler's existing evidence boundary: `public` is a
frozen cross-repository cold-start prior, while online observations accumulate
only under the resolved `repo` key. Dataset metadata is authoritative;
standard `owner__repo-issue` IDs provide a fallback, and unparseable IDs use an
isolated `instance:<id>` key. Thus the three `12rambau__sepal_ui-*` tasks share
`12rambau/sepal_ui` evidence, while another repository sees only its own
evidence and the frozen public layer. Each independent benchmark invocation
starts from the tracked seed for reproducibility.

Default task discovery now prefers `AGENT_TEST_BENCH_ROOT`, then the full
sibling `agent-test-bench` checkout, and uses the bundled four-task file only
as a smoke-test fallback. A request larger than the selected source fails
before execution with an actionable full-dataset message.

The runner exposes a whole-task wall-clock budget as
`--task-timeout-seconds N` (alias `--timeout-seconds N`). The budget starts at
the host task entry, covers setup and normal result collection, and passes only
its remaining time into the agent phase. `--agent-timeout-seconds N` optionally
adds a shorter agent-only bound. Timeout exits use status 124 and agent,
sidecar, and workspace-scoped sandbox cleanup use an independent bounded grace
after the deadline. The old `agent.max_turns` setting was inert: the supported
OpenClaw `agent` CLI does not expose a maximum-turn option.

Full batch JSON remains written to `report.json`, but it is no longer dumped to
the terminal by default. `--json` explicitly enables complete JSON on stdout;
otherwise stderr contains only progress, failure details, and the report path.

Validation completed in the Windows development workspace:

- `python -m pytest tests -q --basetemp .pytest-tmp-full-root`:
  152 passed, 2 skipped.
- `python -m pytest services/scheduler/tests -q --basetemp
  .pytest-tmp-scheduler-root`: 170 passed, 2 skipped.
- `python -m pytest tests/test_swe_rebench_selection.py -q --basetemp
  .pytest-tmp-selection-root-2`: 114 passed, 2 skipped.
- `py_compile` and `git diff --check` pass. Scheduler predictor changed-file
  Ruff passed; the final combined Ruff invocation could not start the
  Microsoft Store `python.exe` alias because Windows reported "the specified
  logon session does not exist", so rerun it in a normal Python environment.
- `python3 scripts/clawtune.py benchmark --sample 32` was not run in this
  Windows workspace because the maintained benchmark requires a Linux host,
  Docker, cgroup v2, and BCC/eBPF. Run it on the target Linux host with the
  full SWE-Rebench task source configured.

## Current Host Stage-2 Fix

### Trusted execution-root attribution (2026-07-29)

The remaining `attribution_gap`,
`sentinel_pre_exec_missing_fork_ancestry`, and `missing_generation` failures
had one integration-specific cause: the fork+exec child posts `/started`
synchronously before `exec`, but Stage-2 can be attached while handling that
request. The collector therefore sees the exec, perf, and exit events while
legitimately missing the earlier launcher-to-child fork.

The authenticated `/started` lifecycle already carries `child_pid`,
`process_starttime_ticks`, and `pid_namespace_inode`. The sidecar now resolves
that tuple to one host PID, stores it on the execution record, and passes it to
the vendored SDK as `trusted_root_pid`. The collector uses this identity as the
command-tree anchor and follows only observed descendant forks. It also uses
the root to isolate concurrent calls, including identical commands in one
shared sandbox cgroup. A root that cannot be verified does not gain this
exception; the existing observed-fork proof and fail-closed behavior remain.

This root is rebound when Stage-2 started early at claim time and retained when
the sandbox container ID arrives after `/started`. Healthy eligible artifacts
continue into `ClauseResourceKB`; ordinary completed tool samples continue
into `RuntimeToolResourceKB`, preserving the empirical latency bucket and
conditional-p90 CPU/memory prediction paths.

### Second-run workspace ownership finding (2026-07-28)

A follow-up `host-openclaw-sandbox` run proved that the coverage clamp and
Docker exec PID attribution changes work, but exposed a second launcher issue:
the root-runner preflight succeeded while 27 real sandbox exec calls failed
with `claw-launch: Permission denied`. The same run's native file tools could
not traverse the repository.

`/testbed` is the repository path in the original SWE-Rebench image. In this
runtime mode it is intentionally copied to a per-task host directory and
mounted by OpenClaw at `/workspace`; changing only the container path back to
`/testbed` would not fix the failure. The actual mismatch was ownership:
`sudo docker cp` created a root-owned tree, the preflight ran as root, and the
real OpenClaw tool container ran unprivileged.

The runner now grants the isolated exported task tree sandbox read/write/search
access without following repository symlinks. The launcher preflight explicitly
runs as numeric uid/gid `65534:65534`, so it exercises the unprivileged access
boundary before an agent run starts.

A third run showed that uid-independent traversal was still not the complete
boundary: the preflight succeeded as uid 65534, while OpenClaw's real exec shell
continued to receive `EACCES` for the 0755 script. This is the behavior of the
real sandbox's non-executable workspace mount, not a missing file mode. The
plugin protocol now has an optional `launcherInterpreter`; this host mode sets
it to `/bin/sh`, producing `/bin/sh /workspace/.claw/bin/claw-launch run ...`.
The workspace remains non-executable and the trusted interpreter reads the
launcher instead. The preflight now uses the identical interpreter chain.

The x86_64 syscall kprobe now unwraps the inner `pt_regs` supplied by
`CONFIG_ARCH_HAS_SYSCALL_WRAPPER` before reading `execve`/`execveat`
arguments. Host preflight also runs a real short-lived cgroup/eBPF collection
and requires non-empty executable/argv events, a successful exec boundary,
zero telemetry loss, and drained lifecycle maps before a full task starts.
The sandbox launcher now uses a non-login `sh -c` payload so host profile
scripts cannot inject unrelated `id -u` images into every command. When
concurrent exec calls overlap in the shared sandbox cgroup, Stage-2 selects an
event tree only when its launcher argv contains the exact registered command;
missing or ambiguous matches still fail closed. Scheduler `exit_code` and SDK
`returncode` completion fields are both accepted, and live calls may resolve
parser-proven `&&`/`||` short circuits from observed clause exit status.
For native OpenClaw tools, a Docker `exec_start` match still takes precedence;
when it is missed, a plugin-provided shared host-runtime scope is now replaced
by the discovered sandbox-container cgroup instead of measuring the unrelated
host OpenClaw process.

## 2026-07-28 Experiment Audit and Fix

The exported `0b01001001__spectree-64` run contains 81 completed LLM spans and
87 completed tool spans. It did enter the tool phase, but all 68 managed
`exec` calls exited 126 before their payload started:

```text
/bin/sh: 1: /workspace/.claw/bin/claw-launch: Permission denied
failureKind=shell-not-executable
```

The host BCC/BPF semantic preflight, module load, cgroup-v2 detection, and
sandbox-container discovery were healthy. Stage-2 reported
`no_active_stage2_run` because the launcher never reached claim/start, so the
runtime KB file was the only JSON file under `tool-resource`; it is not a
clause telemetry artifact. The former audit message `1/68` was therefore a
counting bug. The real coverage was `0/68`.

The launcher installer now makes every private `.claw` path component
traversable, scheduler source readable, and the interpreted launcher exactly
0755. This handles `sudo -E` with a restrictive root umask. Before the model is
started, the runner bind-mounts the workspace into the configured sandbox image
and executes `claw-launch --help`; a permission or interpreter failure now
stops immediately with `sandbox_launcher_preflight_failed`. Trace inspection
also reports `launcher_not_executable` ahead of the secondary `no_patch`
symptom.

The run also exposed 69 invalid `coverage_ratio` values above 1.0 (maximum
about 15.85). Scheduler decision monitoring started earlier than the actual
OpenClaw tool duration, but the trace used the monitor window as the action
window and the shorter tool duration as denominator. Action timestamps are now
derived from the reported tool duration, monitor timestamps remain separate,
and coverage is defensively bounded to `[0,1]`.

Native OpenClaw tools produced three best-effort Docker-event matches, but the
old implementation still sampled the whole container cgroup and labelled it
as exact. The observer now subscribes at `exec_create` as well as `exec_start`,
polls `ExecInspect` at 5 ms while the short process becomes live, correlates the
record to the active tool, and immediately rebinds the monitor to the host PID
and descendants. If the PID is already gone, it keeps the honest shared
sandbox-cgroup fallback. Strict telemetry audits consequently require cgroup-v2
coverage for managed launcher calls and attributed process-or-cgroup samples
for native calls.

Finally, host-sandbox runs discard inherited `OPENCLAW_GATEWAY_*` credentials
before constructing the isolated temporary OpenClaw home. This prevents
`sudo -E` from sending subagent announcements with a token belonging to an
unrelated long-running gateway.

The production predictor remains the vendored `tool_resource` integration:
`ClauseResourceKB` supplies empirical clause latency buckets and
`RuntimeToolResourceKB` supplies causal continuous p90 latency/CPU/memory
estimates. `tool_resource.mlp` is intentionally reported as excluded because
this repository has no trained checkpoint/feature-normalization contract;
running an untrained MLP would fabricate predictions.

## Validation

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root
cd services/scheduler && python -m pytest tests -q
cd packages/openclaw-plugin && npm test && npm run typecheck
python -m pytest services/scheduler/tests/test_sidecar.py \
  services/scheduler/tests/test_tool_runtime_monitor.py -q \
  --basetemp .pytest-tmp-fix-sidecar
python -m pytest tests/test_swe_rebench_runner_inspection.py \
  tests/test_swe_rebench_selection.py -q \
  --basetemp .pytest-tmp-fix-runner
```

Latest Windows validation:

- `$env:PYTHONPATH='services/scheduler/src'; python -m pytest
  services/scheduler/tests -q --basetemp .pytest-tmp-scheduler-20260729b`:
  105 passed.
- `$env:PYTHONPATH='services/scheduler/src'; python -m pytest tests -q
  --basetemp .pytest-tmp-root-20260729b`: 73 passed, 2 Windows/POSIX tests
  skipped.
- `cd packages/openclaw-plugin && npm.cmd test`: 62 passed, including the
  TypeScript build.
- `python tools/validate_contracts.py`: all 9 contract examples passed.
- A parallel full-suite attempt using `--basetemp
  .pytest-tmp-scheduler-all` could not complete because Windows denied pytest
  cleanup of the shared temporary root. The scheduler suite passed when
  rerun sequentially with the unique flat basetemp above.
- A nested `--basetemp .pytest-tmp-scheduler-sequential\base` attempt could
  not run because pytest does not create the missing parent directory. The
  flat basetemp command above is the maintained validation.
- `python -m pytest --basetemp .pytest-tmp-current-fix\basetemp
  services\scheduler\tests\test_tool_resource_telemetry.py
  services\scheduler\tests\test_tool_runtime_monitor.py
  services\scheduler\tests\test_sidecar.py`: 52 passed.
- `cd packages/openclaw-plugin && npm.cmd test -- --runInBand`: 62 passed,
  including TypeScript build.
- `cd swe_rebench\bundle\scheduler && python -m pytest --basetemp
  C:\Users\29068\Desktop\claw\.pytest-tmp-current-fix\bundle-basetemp
  tests\test_sidecar.py`: 38 passed.
- `python -m pytest tests -q --basetemp .pytest-tmp-final2-root`:
  65 passed, 1 POSIX-permission test skipped on Windows.
- Not run in this Windows workspace: full `sudo -E env "PATH=$PATH"
  "$(command -v python3)" -m swe_rebench.runner run --config
  swe_rebench/config.yaml --prepare --dataset swe_rebench/tasks.json --sample
  1 --export --runtime-mode host-openclaw-sandbox`. It requires the Linux/root
  Docker/eBPF host-sandbox environment used by the benchmark runner.
- `$env:PYTHONPATH='services\scheduler\src'; python -m pytest
  services/scheduler/tests -q --basetemp .pytest-tmp-final2-scheduler`:
  101 passed.
- `cd packages/openclaw-plugin && npm.cmd test`: 57 passed, including build
  and trace coverage validation.

The next Linux host-sandbox run is the remaining end-to-end acceptance test.
It must show:

1. `launcher-preflight.log` exits successfully and no exec result contains
   `shell-not-executable`;
2. every executed launcher call has one healthy clause telemetry artifact,
   with clause latency and explicit exit/signaled status;
3. every non-null `coverage_ratio` is in `[0,1]`;
4. native tools use `docker-exec-pid`/`process_tree` when a live Docker exec
   PID is captured, otherwise `shared_sandbox_container`;
5. prediction availability becomes non-zero after causal evidence exists;
   the first cold-start command may correctly remain `unknown`.

## 2026-07-29 Run Audit (Round 10): Fork+Exec — SUCCESS 🎉

### Results: Both Exec Output AND Clause Telemetry Working

The fork+exec launcher approach worked:

| Metric | Before (R1-R9) | Round 10 |
|--------|---------------|----------|
| `healthy_artifact_count` | **0** | **9** ✅ |
| `clause_count` | **0** | **35** ✅ |
| `ok_call_count` | **0** | **9** ✅ |
| `launcher_tool_resource_eligible_span_ends` | **0** | **9** ✅ |
| `matched` | **0** | **3** ✅ |
| Exec output | broken | **working** ✅ |
| Agent patch produced | yes | **yes** (2620 bytes) ✅ |

### Key Diagnostic

```
[telemetry:diag] container_pids=5 matched=3 exec_cgroup_dist=[(1058241, 3)]
cgroup_inodes=[1058241]
```

- `matched=3` — eBPF exec events are now correctly attributed to the container!
- `cgroup_inodes=[1058241]` — single cgroup (exec and init share same cgroup in this run)
- Launcher lifecycle confirmed: claim → started → exited all from container IP (172.17.0.2)

### Why Fork+Exec Works

The child process calls `os.execv("/bin/sh", ...)` — it *becomes* the command.
Docker exec sees the command's stdout directly (no wrapper process in between).
The parent (original launcher) waits and reports exit status to the sidecar.

### Remaining Issues

9/21 healthy (not 21/21). Some calls still produce unhealthy artifacts.
Likely causes: first-call cold start (cgroup discovery not yet cached),
or very short-lived commands where BPF events arrive after the time window.

- `python tools/validate_agent_test_bench_run.py
  C:\Users\29068\Desktop\0b01001001__spectree-64` is not applicable to this
  flat OpenClaw export: the validator searches for files named `trace.jsonl`,
  while the run stores two UUID-named trace-v6 JSONL files. The runner's
  `_inspect_trace`/`_inspect_tool_resource_artifacts` audit was used instead.
- Live SWE-Rebench Docker task execution requires Docker access, real task
  images, and a valid upstream LLM key/model configuration.
- `cd packages/openclaw-plugin && npm test` cannot run directly from this
  Windows PowerShell sandbox because `npm.ps1` is blocked by execution policy;
  use `npm.cmd test` instead.
- `cd swe_rebench/bundle/plugin && npm.cmd run build` cannot run directly
  because the bundled plugin intentionally has no local `node_modules`.
  It was compiled with the main plugin's `tsc` and explicit `--typeRoots`, then
  validated with `node --test test/*.test.mjs`.
- `cd swe_rebench/bundle/plugin && npm.cmd test` cannot run directly for the
  same reason: its `npm test` script invokes local `tsc`, which is absent from
  the intentionally dependency-free bundle directory. Use the main plugin's
  `tsc.cmd -p tsconfig.json --typeRoots ...\packages\openclaw-plugin\node_modules\@types`
  followed by `node --test test/*.test.mjs`.
- `clawhub package validate packages/openclaw-plugin` cannot run because the
  ClawHub CLI is not installed in this Windows workspace. The npm publish file
  set and suspicious static-analysis patterns are checked locally instead.
- `python -m mypy .` currently fails on pre-existing repository-wide typing
  issues, including missing `types-setuptools`, Windows/POSIX launcher
  attribute checks, and trace helper union-attr errors.
- `python -c "import sys; sys.path.insert(0, 'services/scheduler/src'); from tool_resource.features import parse_command_clauses; print(parse_command_clauses('echo hi'))"`
  cannot complete in this Windows workspace because the vendored mvdan adapter
  binary is not built and `go` is not available on PATH to run
  `services/scheduler/src/tool_resource/_mvdan_adapter/build.sh`.
- `.venv\Scripts\python.exe -m pytest services\scheduler\tests\test_tool_resource_predictor.py services\scheduler\tests\test_sidecar.py -q`
  cannot run in the current virtual environment because `pytest` is not
  installed there.
- `.venv\Scripts\python.exe -m pytest services\scheduler\tests\test_tool_resource_predictor.py -q`
  cannot run in the current virtual environment because `pytest` is not
  installed there.
- `python -m pytest services\scheduler\tests\test_tool_resource_predictor.py -q`
  cannot run directly in this Windows sandbox because pytest tries to create
  `C:\Users\29068\.pytest-tmp`, which is outside the writable workspace; use
  `--basetemp .pytest-tmp-root`.
- `python -m pytest services/scheduler/tests/test_tool_resource_predictor.py::test_predictor_retries_stage2_after_container_id_arrives services/scheduler/tests/test_sidecar.py::test_stage2_execution_waits_for_sandbox_container_scope tests/test_swe_rebench_runner_inspection.py -q`
  cannot run directly in this Windows sandbox because pytest tries to create
  `C:\Users\29068\.pytest-tmp`, which is outside the writable workspace; rerun
  with `--basetemp .pytest-tmp-root`.
- `python -m pytest --basetemp C:\tmp\claw-pytest services/scheduler/tests/test_tool_resource_predictor.py::test_predictor_retries_stage2_after_container_id_arrives services/scheduler/tests/test_sidecar.py::test_stage2_execution_waits_for_sandbox_container_scope tests/test_swe_rebench_runner_inspection.py -q`
  cannot run in this sandbox because creating `C:\tmp\claw-pytest` is denied;
  use a basetemp inside the repository, such as `.pytest-tmp-root`.
- `python -m pytest services\scheduler\tests\test_tool_resource_telemetry.py tests\test_swe_rebench_selection.py::test_host_sandbox_sidecar_enables_docker_exec_observer tests\test_swe_rebench_selection.py::test_host_sandbox_writes_tool_resource_preflight tests\test_swe_rebench_runner_inspection.py -q`
  cannot run directly in this Windows sandbox because pytest tries to create
  `C:\Users\29068\.pytest-tmp`, which is outside the writable workspace; rerun
  with `--basetemp .pytest-tmp-root`.
- `python -m pytest services/scheduler/tests/test_tool_resource_telemetry.py
  tests/test_swe_rebench_selection.py -q` and the same command with
  `--basetemp C:\tmp\claw-stage2-pytest-20260728a` cannot run in this Windows
  sandbox because those temp roots are not writable; the command passed with a
  repository-local `--basetemp .pytest_tmp_stage2_20260728a`.
- `python -m pytest services/scheduler/tests/test_launcher.py -q` and
  `python -m pytest services/scheduler/tests/test_tool_resource_telemetry.py
  -q` cannot use this sandbox's default `C:\Users\29068\.pytest-tmp`.
- The same two targeted commands with `--basetemp
  C:\tmp\claw-pytest-launcher` and `--basetemp
  C:\tmp\claw-pytest-telemetry` cannot create those temp roots. Both suites
  pass with repository-local basetemp directories.
- `python -m pytest --basetemp C:\tmp\claw-pytest services\scheduler\tests\test_tool_resource_telemetry.py tests\test_swe_rebench_selection.py::test_host_sandbox_sidecar_enables_docker_exec_observer tests\test_swe_rebench_selection.py::test_host_sandbox_writes_tool_resource_preflight tests\test_swe_rebench_runner_inspection.py -q`
  cannot run in this sandbox because creating `C:\tmp\claw-pytest` is denied;
  use a basetemp inside the repository, such as `.pytest-tmp-root`.
- The Linux cgroup v2 migration probe and `systemd-run --user --scope -p
  Delegate=yes ... openclaw agent ...` validation cannot run in this Windows
  PowerShell workspace; they must be run on the Linux host/container where
  `/sys/fs/cgroup` is mounted.
- `python -m ruff check swe_rebench\host_sandbox.py swe_rebench\runner.py
  services\scheduler\src\agent_scheduler\api\app.py
  services\scheduler\src\agent_scheduler\predictors\tool_resource.py
  services\scheduler\src\tool_resource\clause_bridge.py
  services\scheduler\src\tool_resource\sdk.py
  services\scheduler\src\tool_resource\telemetry.py
  tests\test_swe_rebench_runner_inspection.py
  tests\test_swe_rebench_selection.py
  services\scheduler\tests\test_sidecar.py
  services\scheduler\tests\test_tool_resource_predictor.py
  services\scheduler\tests\test_tool_resource_telemetry.py
  tools\validate_contracts.py` cannot run because `ruff` is not installed in
  the active Windows Python environment.
- The complete `sudo -E env "PATH=$PATH" "$(command -v python3)" -m
  swe_rebench.runner run --config swe_rebench/config.yaml --prepare --dataset
  swe_rebench/tasks.json --sample 1 --export --runtime-mode
  host-openclaw-sandbox` validation cannot run in this Windows workspace
  because it requires a Linux host with cgroup v2, BCC, BPF/perf permissions,
  Docker, OpenClaw, and the configured upstream model.
- `docker version --format '{{json .}}'` and a live
  `container-openclaw` smoke run cannot run in this Windows workspace because
  the Docker CLI/daemon is not installed.
- `bash -n .container-audit-bundle/entrypoint.sh`,
  `bash -n .container-audit-bundle/setup.sh`, and
  `bash -n .container-audit-bundle/run_agent.sh` cannot run in this Windows
  workspace because WSL instance creation is denied with
  `Wsl/Service/CreateInstance/E_ACCESSDENIED`; validate the generated scripts
  on the Linux Docker host.
- `bash -n swe_rebench/bundle/entrypoint.sh` and
  `bash -n swe_rebench/bundle/run_agent.sh` (CI does this for the tracked
  bundle) cannot run in this Windows workspace because `bash` resolves to WSL
  and no WSL distribution is installed; validate on the Linux host after the
  `openclaw agent` CLI-syntax fix regenerates the bundle.
- `bash -n scripts/setup/arm_qemu_setup.sh` cannot run in this Windows
  workspace because WSL instance creation is denied with
  `Wsl/Service/CreateInstance/E_ACCESSDENIED`; validate the ARM/QEMU setup
  script on the Kunpeng Linux Docker host.
- `sudo bash scripts/setup/arm_qemu_setup.sh install`, `sudo bash
  scripts/setup/arm_qemu_setup.sh check`, and any live `docker run --platform
  linux/amd64 ...` smoke cannot run in this Windows workspace because they
  require a Linux ARM host with Docker, privileged binfmt registration, and
  QEMU user emulation.
- `python -m pytest -q --basetemp .pytest-tmp-container-root` cannot run as one
  repository-wide collection command because the scheduler package requires
  `services/scheduler/src` on `PYTHONPATH` and the generated
  `swe_rebench/bundle/scheduler/tests` tree duplicates scheduler test module
  names. The maintained suites were run separately instead.
- `npm test` from `packages/openclaw-plugin` cannot run through PowerShell in
  this Windows workspace because `npm.ps1` is blocked by the local execution
  policy. The equivalent `npm.cmd test` command was run instead.
- `npm run build` from `packages/openclaw-plugin` cannot run through
  PowerShell in this Windows workspace because `npm.ps1` is blocked by the
  local execution policy. The equivalent `npm.cmd run build` command was run
  instead.

## 2026-07-29 Host-Sandbox Launcher Regression Audit

The pasted failing run was made after commit `51aa043` forced every launcher
back through `_run_subprocess`. Its symptoms match that regression: managed
exec calls became long-running OpenClaw process sessions, produced no command
output, and all 12 finalized Stage-2 artifacts were invalid with zero mapped
clauses. The separately downloaded flat export is from a different run: it has
`agent_exit_code=0` and a non-empty patch, but it does not include the
`tool-resource/` artifacts needed to audit eBPF eligibility.

Host-openclaw-sandbox now explicitly selects `CLAW_LAUNCH_MODE=fork-exec`.
The plugin forwards that variable into the Docker exec environment; other
deployment modes retain the subprocess path with its cgroup, affinity, NUMA,
and systemd-scope behavior. The forked payload reports the parent launcher and
child identities separately, scrubs execution credentials before `execve`,
forwards cancellation signals, and reports signal-derived shell exit status.
The required host-sandbox audit now also fails when prediction envelopes are
missing or when no latency/CPU/memory estimate becomes usable.

Validation in this Windows workspace:

- `cd packages/openclaw-plugin && npm.cmd test`: 62 passed, including the new
  launcher-mode environment forwarding assertion.
- `git diff --check`: passed.
- Python unit tests could not run because this execution environment exposes
  only the non-functional Windows Store `python.exe` alias and has no Python
  interpreter on `PATH`.
- The live `host-openclaw-sandbox` acceptance command could not run here
  because it requires the user's Linux host with Docker, cgroup v2, BCC/eBPF
  privileges, OpenClaw, and the configured upstream LLM credentials.

### Follow-up run at 09:03 UTC

The next pasted Linux run still did not enter the fork lifecycle. All four
managed executions reached `/v2/executions/claim`, but the sidecar log contains
no `/started` or `/exited` request. The eBPF diagnostic consequently reported
`matched=0`; all four calls had zero mapped clauses and produced unhealthy
artifacts. Prediction envelopes were present for all 17 tool calls and two
continuous predictions were usable, so prediction integration was alive but
could not receive new clause evidence.

The configured `CLAW_LAUNCH_MODE` is now forwarded by the plugin and the
launcher exposes a `diagnose` command. Host-sandbox preflight executes that
command inside the mounted task image with `CLAW_LAUNCH_MODE=fork-exec`; a
stale bundle, unsupported platform, missing `os.fork`, or wrong mode now stops
before the model run. Artifact inspection also reports aggregated
`invalid_reason_counts` and the underlying invalid-reason details.

The supplied Windows directory
`C:\Users\user\Desktop\0b01001001__spectree-64` was empty at inspection time,
so the four Stage-2 JSON files referenced by the pasted report were not
available for a deeper offline audit.

The directory was subsequently populated and confirmed both findings. The
copied plugin source/dist contains no `CLAW_LAUNCH_MODE`, and
`launcher-preflight.log` exposes only the old `{run}` command, proving this run
predates the mode-forwarding and `diagnose` changes. All four collectors were
kernel-healthy (`cleanup=ok`, active before close, zero unavailable calls, and
tens of thousands of kprobe hits), but every call failed analysis with
`disconnected_command_trees`. The artifacts contain dozens to hundreds of
unrelated host roots because the fallback added every system-wide exec
boundary's cgroup merely for overlapping the tool window.

Dynamic cgroup discovery now accepts a cgroup only when an event is tied to an
already authenticated/container PID (including the launcher trusted root) or
the container PID namespace. A pure unit test ensures unrelated exec
boundaries are rejected. This makes a missing `/started` lifecycle fail as
missing target evidence instead of contaminating telemetry with host process
trees; the fork-mode preflight prevents that state in the maintained
host-sandbox route.

## 2026-07-29 Clause Status Source Contract Alignment

Stage-2 telemetry can use `live_shell_exit_code` when a live shell reports a
command-lookup failure. The public clause-telemetry schema now includes that
value in `clauseStatus.source`, and the maintained example exercises it on a
`no_runtime_exec` status.

Validation in this Windows workspace:

- A focused Node JSON/enum fixture check passed for both
  `root_exec_chain_terminal` and `live_shell_exit_code`.
- `git diff --check -- contracts/clause-telemetry.schema.json
  contracts/examples/clause-telemetry.json`: passed.
- `python tools\validate_contracts.py`: passed for all nine schema examples
  using the host Python interpreter.

## 2026-07-29 Spectree Host-Sandbox Final Audit

The downloaded `0b01001001__spectree-64` run proves that fork/exec and eBPF
collection were active: all 15 launcher executions have a claim/start/exit,
one trusted launcher root, a connected command tree, healthy active-to-closed
collectors, positive kprobe hits, and zero ring/telemetry loss. Twelve calls are
Clause-KB eligible. The other three are explicit command-semantic rejections
(one shell parse failure and two masked missing pip commands), not collector
failures, and contributed no Clause-KB observations.

The maintained host route now selects the task image's testbed Python before
system Python, removes the scheduler-only `PYTHONPATH` before payload exec, and
installs `pip`/`pip3` wrappers backed by `python3 -m pip`. A sandbox runtime
preflight checks Python and both pip entry points before the agent runs. Stage-2
completion consumes bounded raw stdout/stderr before any async scope wait;
telemetry GET is read-only, so it cannot race completion and discard masked
command-lookup diagnostics. Fallback finalization uses the predictor's active
run as its exactly-once authority.

The required runner gate now separates artifact envelopes, collector health,
trusted-root lifecycle, command semantics, and KB eligibility. It requires
schema-valid call quality, explicit non-OK reasons, launcher exit status,
one-to-one trace execution/tool-call references to disk artifacts, and usable
finite evidence-backed bucket plus latency/CPU/memory predictions. Applying
that stricter gate to the downloaded run gives 15/15 collector/artifact/
lifecycle/reference coverage, 12 eligible + 3 explicitly rejected calls,
9 bucket predictions, 6 predictions for each continuous target, no warnings,
and no gate error.

Validation in this Windows workspace:

- `python -m pytest tests -q --basetemp .pytest-final-root`: 79 passed,
  2 skipped.
- `python -m pytest services\scheduler\tests -q --basetemp
  .pytest-final-scheduler`: 117 passed.
- `npm.cmd test` from `packages/openclaw-plugin`: 62 passed; its TypeScript
  build also passed.
- `python tools\validate_contracts.py`: all nine examples passed.
- Focused Ruff, `py_compile`, and `git diff --check`: passed.

The post-fix live command below cannot run in this Windows workspace because it
requires the user's Linux Docker host, cgroup v2, BCC/eBPF privileges, OpenClaw,
and configured upstream LLM credentials. It remains the final acceptance run:

```bash
sudo -E env "PATH=$PATH" "$(command -v python3)" \
  -m swe_rebench.runner run \
  --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export \
  --runtime-mode host-openclaw-sandbox
```

## 2026-07-29 Spectree Compound-Prediction and Payload-Environment Follow-up

The newly downloaded run contains 22 launcher Stage-2 artifacts and all 22
collectors are healthy: claim/start/exit coverage is complete, each artifact
has a trusted launcher root, and no telemetry or ring-buffer loss is reported.
Twenty-one calls are Clause-KB eligible. The sole invalid call is the
long-running `pip | tail` execution: OpenClaw reported `status=running` after
ten seconds, and Stage-2 finalized before the later real launcher exit, leaving
the `pip` and `tail` images with `no_causal_end`.

The run failed only at the required prediction gate. It recorded 33 prediction
envelopes and 23 usable continuous latency/CPU/memory predictions, but zero
command-level clause buckets. Every launcher-backed shell command was compound
(for example, `cd /workspace && python3 ...`), and the predictor deliberately
refused to invent a single top-level duration bucket without a valid sequential
or pipeline composition rule. The old response exposed no independent clause
outcomes, so the runner could not recognize the real per-clause evidence.

The sandbox runtime preflight itself selected the testbed interpreter, but the
actual OpenClaw exec payloads resolved `python3` to `/usr/bin/python3`; that
explains the missing `pydantic`, `pip`, and `pytest` seen in the agent output.
The preflight had injected PATH only into its standalone Docker command, while
the actual OpenClaw sandbox exec did not inherit that override.

The maintained path now addresses all three causes:

- The public tool-decision JSON Schema requires `clause_predictions`. A
  compound command keeps its top-level prediction unavailable, while every
  exec-producing clause returns either a real evidence-backed bucket or an
  explicit unavailable reason. Shell builtins such as `cd` and `export` retain
  their clause indexes but do not require impossible eBPF exec evidence. The
  runner accepts a valid per-clause bucket without pretending it is a composed
  command duration.
- The host route seeds both runtime and clause snapshots. The clause snapshot
  is an explicit synthetic public/global cold-start prior (16 observations at
  1200 ms); it is advisory evidence, not learned task-specific truth, and is
  superseded by causal repo evidence as completed calls become available.
- `claw-launch` exports the complete testbed-first PATH before starting the
  scheduler, while OpenClaw also receives `tools.exec.pathPrepend`. Launcher
  diagnostics now report the exact payload `python3`, `pip`, and `pip3`, and
  Python-task preflight fails unless they resolve to the testbed interpreter
  and mounted wrappers.
- The required host route denies the OpenClaw `process` tool so exec remains a
  single synchronous lifecycle. Independently, a completion event whose raw
  result is still `status=running` no longer finalizes Stage-2; the later real
  `/exited` event owns completion and the existing grace fallback.
- Task manifests now record the resolved runner config and bundle source
  fingerprint, and the bundle fingerprint includes the contracts and shipped
  tool-resource snapshots. This makes a stale Linux bundle visible and causes
  `--prepare` to rebuild it.

Validation in this Windows workspace after integration:

- `python -m pytest tests -q --basetemp .pytest-tmp-final-root-new`: 83 passed,
  2 skipped.
- `python -m pytest services\scheduler\tests -q --basetemp
  .pytest-tmp-final-scheduler-proof`: 120 passed (one third-party Starlette
  deprecation warning).
- `npm.cmd test` from `packages/openclaw-plugin`: TypeScript build passed and
  all 62 tests passed.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- `python -m py_compile` for every changed production Python module, focused
  Ruff for all changed Python and test files, and `git diff --check`: passed.

Project-wide static baselines that cannot pass unchanged are recorded here as
required. `python -m ruff check .` reports 14 existing errors confined to
`scripts/check_cgroup.py`, `scripts/debug_cgroup.py`, and
`scripts/remote_diag.py`. `python -m mypy .` reports 238 existing/project-wide
errors, including unavailable BCC/setuptools/jsonschema stubs, Windows typing
for POSIX-only launcher APIs, generated-bundle duplicates, and existing typed
data-shape issues. Neither command identified a focused Ruff failure in this
change; mypy is not currently configured as a clean repository-wide gate.

The external trace copy was inspected read-only and was not modified. The live
post-fix acceptance run still cannot execute in this Windows workspace because
it requires the user's Linux Docker/cgroup-v2/BCC environment and credentials;
rerun the same `--prepare --runtime-mode host-openclaw-sandbox` command above
to prove the final end-to-end route.

## 2026-07-29 Runtime Mode Audit

The `host-openclaw-sandbox` path is the maintained complete telemetry route:
the runner keeps OpenClaw and the sidecar on the host, exports `/testbed` from
the task image into a host workspace, tags the task image as the OpenClaw
sandbox image, and runs tools through the host OpenClaw Docker sandbox. Passing
`--runtime-mode host-openclaw-sandbox` also makes Stage-2 telemetry required
unless `--no-stage2-required` is supplied.

The `container-openclaw` path runs OpenClaw, the plugin, and the sidecar inside
each SWE-Rebench task image through `/claw/entrypoint.sh`. It is now an
explicit legacy/diagnostic mode rather than the public default. Its main
failure surface is setup-heavy:
the task image must be able to install or already contain Node/OpenClaw,
sidecar Python dependencies, Docker CLI access to the mounted daemon socket,
and optionally cgroup/BCC tooling.

This audit found one local `--prepare` blocker: `swe_rebench.prepare` copied
`services/scheduler/.pytest-tmp-root`, and Windows denied access to that
temporary directory. The scheduler bundle copy now skips `.pytest-tmp*`
directories, matching the existing treatment of `.pytest_cache` and generated
artifacts.

Validation in this Windows workspace:

- `python -m swe_rebench.runner run --config swe_rebench/config.yaml
  --dataset swe_rebench/tasks.json --sample 1 --runtime-mode
  container-openclaw --dry-run`: passed.
- `python -m swe_rebench.runner run --config swe_rebench/config.yaml
  --dataset swe_rebench/tasks.json --sample 1 --runtime-mode
  host-openclaw-sandbox --dry-run`: passed.
- `python -m swe_rebench.runner prepare --config swe_rebench/config.yaml`:
  initially failed with `PermissionError` copying
  `services/scheduler/.pytest-tmp-root`; passed after skipping `.pytest-tmp*`.
- `python -m pytest tests\test_swe_rebench_selection.py
  tests\test_swe_rebench_runner_inspection.py`: cannot use the default
  Windows temp root because access to
  `C:\Users\29068\AppData\Local\Temp\pytest-of-29068` is denied. The same
  suite passed with `--basetemp .pytest-tmp-runtime-audit`.
- `python -m pytest tests\test_swe_rebench_selection.py
  tests\test_swe_rebench_runner_inspection.py -q --basetemp
  .pytest-tmp-runtime-audit`: 80 passed, 2 skipped.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- `git diff --check`: passed.

Still not runnable here: a live `container-openclaw` or
`host-openclaw-sandbox` SWE-Rebench task execution, because this Windows
workspace lacks the Linux Docker/cgroup/eBPF host environment, task-image
runtime, and upstream LLM credentials used by the benchmark runner.

## 2026-07-29 Container-OpenClaw Recovery

The downloaded `spectree` run reached Agent turn 8, then left a managed
`pip install ...; pip list ...` execution without a launcher `/exited` event.
The repair is isolated to the container route and the shared subprocess
launcher path; `host-openclaw-sandbox` keeps its existing fork-exec runtime.

- Container entrypoint now selects and exports the task Python, prepends its
  bin directory, and ships `pip`/`pip3` wrappers that execute the same Python.
  The launcher receives `CLAW_TASK_PYTHON` explicitly and reconstructs that
  payload PATH after removing its scheduler-only `PYTHONPATH` entry.
- The generated OpenClaw patch uses top-level `tools.exec.pathPrepend`, which
  matches the current OpenClaw schema. A failed config patch is now fatal and
  visible in `phase3.log` instead of silently running with plugin defaults.
- Subprocess launchers now report `/started` once, after the payload PID is
  known. This removes the former cgroup-path first report followed by a
  trusted-root-changing second report (HTTP 409).

Validation in this Windows workspace:

- `python -m pytest tests/test_swe_rebench_selection.py
  tests/test_swe_rebench_runner_inspection.py -q --basetemp
  .pytest-tmp-container-repair-root`: 80 passed, 2 skipped.
- `PYTHONPATH=src python -m pytest tests/test_launcher.py -q --basetemp
  ../../.pytest-tmp-container-repair-scheduler` from `services/scheduler`:
  28 passed.
- `npm.cmd test` from `packages/openclaw-plugin`: 62 passed.
- `python -m swe_rebench.runner prepare --config swe_rebench/config.yaml` and
  both runtime-mode `--dry-run` commands passed; generated bundle JSON parsed
  successfully and `git diff --check` passed.

Still not runnable here: the live acceptance command for either mode requires
the user's Linux Docker/cgroup-v2 environment, the task image runtime, and
LLM credentials. In particular, the live command required to validate this
container recovery is:

```bash
sudo -E env "PATH=$PATH" "$(command -v python3)" \
  -m swe_rebench.runner run \
  --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export \
  --runtime-mode container-openclaw
```

## 2026-07-29 Container-OpenClaw Live Output

`container-openclaw` now mirrors the agent's stdout and stderr to both its
existing trace files and the container log. The Docker SDK and CLI fallback
follow that log while waiting, so the invoking terminal receives phase and
agent progress without changing the `host-openclaw-sandbox` path.

Validation in this workspace:

- `python -m pytest tests/test_swe_rebench_selection.py -q --basetemp
  .pytest-tmp-live-logs`: 68 passed, 2 skipped.
- `python -m swe_rebench.runner prepare --config swe_rebench/config.yaml` and
  both `container-openclaw` / `host-openclaw-sandbox` `--dry-run` commands:
  passed.
- `git diff --check`: passed.

The same live Linux acceptance command above remains unavailable from this
Windows workspace and is required to verify real Docker log timing.

## 2026-07-29 Container-OpenClaw eBPF Recovery Follow-up

The container bootstrap explicitly installs `libelf1` with the BCC packages.
The generated preflight and the Stage-2 container-cgroup resolver now retain
Docker CLI as the first choice but fall back to the already-mounted Docker Unix
socket when the image's CLI is too old for the host daemon. This is isolated to
the container route; `host-openclaw-sandbox` keeps its existing startup and
fork-exec telemetry path.

Validation in this Windows workspace:

- `python -m pytest tests\\test_swe_rebench_selection.py -q --basetemp
  .pytest-tmp-container-socket`: 68 passed, 2 skipped.
- `PYTHONPATH=src python -m pytest tests\\test_tool_resource_telemetry.py -q
  --basetemp ..\\.pytest-tmp-container-socket-scheduler` from
  `services\\scheduler`: 17 passed.
- `python -m swe_rebench.runner prepare --config swe_rebench/config.yaml`, and
  both runtime-mode `--dry-run` commands: passed.
- `git diff --check`: passed.

Validation unavailable in this Windows workspace:

- A live `container-openclaw` run remains unavailable because it requires the
  Linux Docker/cgroup-v2/eBPF environment, task image, and model credentials.
  Run the documented acceptance command and require both
  `bcc_import.ok: true` and `stage2_ready: true` in
  `tool_resource_preflight.json`; then confirm the tool-resource artifacts no
  longer report `collector_disabled`.

## 2026-07-30 Git-Safe Runtime Output

The default SWE-Rebench bundle and all generated run outputs now live under
`swe_rebench/.runtime/`, which is ignored by Git. This prevents a root-required
live runner from rewriting tracked `swe_rebench/bundle` files and blocking a
server-side fast-forward-only pull. Git operations must remain unprivileged;
if an earlier run already created root-owned repository files, repair ownership
once with `sudo chown -R <user>:<group> <repo>` before using Git normally.

## 2026-07-30 Container-OpenClaw libelf Payload Repair

The latest live `container-openclaw` run completed the agent workload, produced
a patch, and preserved full launcher/cgroup attribution, but every Stage-2
artifact was rejected because importing BCC still failed with
`libelf.so.1: cannot open shared object file`. The setup log showed BCC packages
being installed without `libelf1` being unpacked even though the container
template explicitly requested it. This is consistent with a minimized task
image retaining dpkg package metadata after the shared-library payload was
removed: a normal install treats the package as already present and does not
restore the file.

The container setup now probes the selected Python/BCC binding after package
installation. Only when that probe specifically reports missing
`libelf.so.1`, the apt path force-reinstalls `libelf1` and refreshes the dynamic
linker cache. The retry stays best-effort, and all changes are confined to the
container setup template/prebuilt setup script; `host_sandbox.py`, host
preflight, and scheduler telemetry code are unchanged.

The same setup log exposed a separate container-only portability bug: its
hard-coded Node 24 patch URL returned HTTP 404, and the fallback archive pattern
was fixed to x64. Setup now resolves the current v24 archive from
`SHASUMS256.txt` using the detected x64/arm64 architecture. Root runtime-mode
tests are now part of CI, and the host dispatch regression test explicitly
fails if host mode enters the container runner.

Validation in this Windows workspace:

- `python -m pytest tests -q --basetemp
  .pytest-tmp-libelf-final-root`: 86 passed, 2 skipped.
- With `PYTHONPATH=src`, the scheduler launcher, sidecar, tool-resource
  predictor, and telemetry suites: 110 passed.
- `npm.cmd test` from `packages/openclaw-plugin`: 62 passed.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- `python -m swe_rebench.runner prepare --config swe_rebench\config.yaml`:
  passed; generated `setup.sh` matches the tracked template.
- Both `container-openclaw` and `host-openclaw-sandbox` one-task dry-runs:
  passed.
- Python compilation, CI YAML parsing, and `git diff --check`: passed.

Validation unavailable in this Windows workspace:

- The live `container-openclaw` acceptance command cannot run because the
  Docker CLI is not installed here (`docker version` is not recognized).
- A WSL fallback is also unavailable: `wsl.exe --status` returns
  `Wsl/EnumerateDistros/Service/E_ACCESSDENIED`.
- `bash -n swe_rebench/bundle/setup.sh` cannot run locally for the same WSL
  access reason; it is now an Ubuntu CI step.
- Focused Ruff validation could not run because neither the system Python nor
  `.venv` has the `ruff` module installed.
- The next Linux acceptance run must show
  `[claw] libelf.so.1 is missing; reinstalling libelf1...`, followed by
  `[claw] libelf1 reinstall repaired the BCC runtime` and
  `[claw] BCC Python binding OK`; `tool_resource_preflight.json` must then
  report `bcc_import.ok: true`. If `stage2_ready` remains false, record its
  separate Docker/cgroup/kernel precondition rather than treating the libelf
  repair as failed.
- The focused pytest command with
  `--basetemp C:\tmp\claw-runtime-boundary-pytest` could not use that directory
  because of local ACLs; rerunning the same selection below a writable
  per-user temp directory passed.

## 2026-07-30 Container-OpenClaw Conda libstdc++ Isolation

The latest downloaded live run is internally consistent: container
`9f4a528d5930`, trace
`0a052268-1ef4-4936-bfa7-e24073972a58_ef86c391-8073-4703-8d2f-b49336f58fb9`,
and the 1,927-byte model patch all match the terminal report. It confirms that
the libelf repair, Node 24 resolution, Docker Unix-socket fallback, cgroup
attribution, and launcher lifecycle are working. In particular,
`docker_inspect` succeeded via `docker-unix-socket`, and all 11 launcher calls
had exit status, cgroup attribution, Stage-2 lifecycle, and artifact references.

Stage-2 remained unavailable because the selected Conda Python loaded
`/opt/conda/lib/libstdc++.so.6`, which lacks `GLIBCXX_3.4.30` required by the
system `/lib/x86_64-linux-gnu/libclang-cpp.so.14`. All 11 Stage-2 artifacts
therefore reported `collector_disabled`; this is an ABI bootstrap failure, not
a Docker-socket or launcher-lifecycle failure.

The container setup now handles only that exact
`libstdc++.so.6`/`GLIBCXX_*`/`not found` signature. It enumerates system
`libstdc++.so.6` candidates from `ldconfig`, accepts only readable paths below
`/lib`, `/lib64`, `/usr/lib`, or `/usr/lib64`, and verifies each candidate with
a real BCC import. A successful candidate is recorded in a root-created mode
`0600` marker. After sidecar dependencies are installed, a combined
FastAPI/uvicorn/pydantic/psutil/numpy/BCC import must also pass with that
candidate; otherwise the marker is removed and Stage-2 remains fail-open. A
fresh or resumed setup clears any stale marker before probing, while an
idempotent `SETUP_DONE` return preserves the already-verified marker.

The entrypoint does not export `LD_PRELOAD` or use `LD_LIBRARY_PATH`. It reads
the validated marker into a Bash environment array and passes it only to the
tool-resource preflight and the sidecar process. The cgroup probe, scheduler
installation, OpenClaw, `claw-launch`, task Python, and agent payload do not
receive it. `host-openclaw-sandbox`, shared scheduler code, and
`services/scheduler/src/tool_resource` are unchanged.

Validation in this Windows workspace:

- `python -m pytest tests -q -p no:cacheprovider --basetemp
  .pytest-tmp-libstdcxx-full`: 88 passed, 2 skipped.
- The focused container repair, tracked/generated bundle, host dispatch, and
  host sidecar environment selection: 6 passed.
- `python -m swe_rebench.runner prepare --config
  swe_rebench\config.yaml`: passed. Generated and tracked setup scripts match
  `_SETUP_TEMPLATE`; generated and tracked default entrypoints match the
  rendered `_ENTRYPOINT_TEMPLATE`.
- One-task dry-runs for both `container-openclaw` and
  `host-openclaw-sandbox`: passed.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- Python compilation, CI YAML parsing, and `git diff --check`: passed.

Validation unavailable in this Windows workspace:

- `bash -n swe_rebench/bundle/setup.sh` and
  `bash -n swe_rebench/bundle/entrypoint.sh` both fail before parsing. The
  sandboxed attempt returns `Bash/Service/CreateInstance/E_ACCESSDENIED`; the
  approved out-of-sandbox retry reaches WSL but reports that no Linux
  distribution is installed. Both commands are Ubuntu CI steps.
- A local tree-sitter Bash parse fallback also cannot run because the installed
  `tree_sitter_languages` package is incompatible with the installed
  `tree_sitter` API (`TypeError: __init__() takes exactly 1 argument (2
  given)`).
- A live Linux `container-openclaw` run remains unavailable locally because
  this workspace has no usable Docker/cgroup-v2/eBPF runtime or model
  credentials.

The next Linux acceptance run must show the sidecar-only system-libstdc++
repair message and `BCC Python binding OK`. Its
`tool_resource_preflight.json` must report `docker_inspect.ok: true`,
`bcc_import.ok: true`, a system `bcc_ld_preload`, and `stage2_ready: true`.
At least one launcher artifact must stop reporting `collector_disabled`. If
BCC import succeeds but attach or compilation then fails, record that new error
separately; the host kernel is 6.8 while the task image currently installs
Debian 6.1 headers, so matching kernel headers may be the next objective
container limitation. Do not pre-emptively mount or mutate host kernel-header
trees without live evidence.

## 2026-07-30 Container-OpenClaw Host Kernel Headers

The subsequent Linux evidence identified that objective mismatch. The running
host kernel is `5.15.0-179-generic`, and its build link resolves to the existing
host directory `/usr/src/linux-headers-5.15.0-179-generic`; the container
instead installed Debian 6.1 headers. BCC imports successfully after the scoped
libstdc++ repair, but cannot compile a BPF module without headers for the
running host kernel.

`run_container` now discovers headers only for a local Linux Docker daemon. It
mounts the exact `/lib/modules/<uname -r>` directory and the exact resolved
`build` target at their identical container paths, both read-only. The build
target must resolve to an existing directory below `/usr/src`; malformed kernel
release values, missing paths, resolution errors, targets outside `/usr/src`,
non-Linux runners, and remote Docker daemons all skip the mounts. This preserves
the existing best-effort Stage-2 fallback instead of making container launch
depend on host-header availability.

The change is confined to the container runner. `host-openclaw-sandbox`, shared
scheduler code, JSON Schema, OpenClaw core, and
`services/scheduler/src/tool_resource` are unchanged.

Validation in this Windows workspace:

- `python -m pytest tests/test_swe_rebench_selection.py -q -p
  no:cacheprovider --basetemp .pytest-tmp-kheaders`: 76 passed, 2 skipped.
- `python -m pytest tests -q -p no:cacheprovider --basetemp
  .pytest-tmp-kheaders-full`: 91 passed, 2 skipped.
- `python -m compileall -q swe_rebench tests`: passed.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- `git diff --check`: passed.

Validation unavailable in this Windows workspace:

- A live Linux `container-openclaw` run cannot be performed here because this
  workspace has no usable Linux Docker/cgroup-v2/eBPF runtime or model
  credentials. The next host run must log
  `container kernel headers: mounting host
  /lib/modules/5.15.0-179-generic and
  /usr/src/linux-headers-5.15.0-179-generic read-only`. Its preflight should
  then list the matching header directory. A later verifier/attach failure
  should be treated as a separate kernel capability limitation.

## 2026-07-30 Container-OpenClaw Tracefs Mount-Namespace Fix

The latest downloaded live run advances beyond every earlier container
bootstrap failure. Container `c1fc330b09fd` completed the agent workload and
produced a 2,316-byte patch. `tool_resource_preflight.json` confirms root,
cgroup v2, Docker inspection through the Unix socket, BCC import, the scoped
system `libstdc++`, and matching `5.15.0-179-generic` host headers. It
incorrectly reported `stage2_ready: true`, however, while every one of the 19
tool-resource artifacts was withheld.

The decisive sidecar error is:

`open(/sys/kernel/tracing/events/sched/sched_process_exit/id): No such file or directory`

This is a mount-namespace failure, not another package, ABI, header, cgroup, or
BPF-permission failure. Docker `--privileged` grants the required capabilities
but does not propagate the host tracefs mount into the task container.

The container runner now inspects only the two standard local host tracefs
roots. For a local Unix-socket Docker daemon, it selects the first root that
actually contains `events/sched/sched_process_exit/id` and `kprobe_events`,
then bind-mounts that exact root at the same container path read-write.
Read-write is required because BCC can create dynamic kprobe events. Non-Linux
runners, remote Docker daemons, roots without the required controls, and
filesystem inspection errors add no mount and preserve the existing
best-effort fallback.

The generated container preflight now records the selected tracefs path,
visibility of `sched_process_exit`, and whether `kprobe_events` is writable.
`stage2_ready` cannot be true unless both conditions hold, eliminating the
false-positive readiness state from this run. The change is confined to
`container-openclaw`; `host-openclaw-sandbox`, OpenClaw core, JSON Schema
contracts, shared scheduler behavior, and
`services/scheduler/src/tool_resource` are unchanged.

Validation in this Windows workspace:

- `python -m pytest tests\test_swe_rebench_selection.py -q -p
  no:cacheprovider --basetemp .pytest-tmp-tracefs`: 78 passed, 2 skipped.
- `python -m pytest tests -q -p no:cacheprovider --basetemp
  .pytest-tmp-tracefs-full`: 93 passed, 2 skipped.
- With `PYTHONPATH=services\scheduler\src`,
  `python -m pytest services\scheduler\tests -q -p no:cacheprovider
  --basetemp .pytest-tmp-tracefs-scheduler`: 122 passed.
- `python -m compileall -q swe_rebench tests`: passed.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- `python -m swe_rebench.runner prepare --config
  swe_rebench\config.yaml`: passed; the tracked and generated entrypoints and
  the tracked/current source fingerprints match.
- One-task `--dry-run` commands for both `container-openclaw` and
  `host-openclaw-sandbox`: passed.
- `git diff --check`: passed.

Validation unavailable in this Windows workspace:

- The user's live acceptance command cannot run here because `docker` is not
  installed and this host is not a Linux cgroup-v2/eBPF environment:
  `sudo -E env "PATH=$PATH" "$(command -v python3)" -m swe_rebench.runner run
  --config swe_rebench/config.yaml --dataset swe_rebench/tasks.json --sample 1
  --export --runtime-mode container-openclaw`.
- The next Linux run must log `container tracefs: mounting host
  /sys/kernel/tracing read-write` (or the standard
  `/sys/kernel/debug/tracing` alternative). Its
  `tool_resource_preflight.json` must contain
  `tracefs.sched_process_exit: true`,
  `tracefs.kprobe_events_writable: true`, and `stage2_ready: true`; at least
  one launcher artifact must no longer report
  `collector_disabled ... sched_process_exit ... No such file or directory`.

## 2026-07-30 Container-OpenClaw Pre-Exec Stage-2 Gate

The next downloaded live run confirms that the tracefs fix worked:
container `f9f4075559ee` reported matching host headers, BCC import success,
visible `sched_process_exit`, writable `kprobe_events`, and
`stage2_ready: true`. All 16 Stage-2 artifacts had an active, healthy
collector, zero ring loss, zero argv loss, clean shutdown, and hundreds of
kprobe hits.

Nevertheless, all 16 calls were rejected with
`ClauseTelemetryIntegrityError ... reason=no_exec_images`. The per-call
diagnostics make the new race conclusive: each collector's target cgroup inode
contained no exec event, while its system-wide event stream contained execs
from neighboring concurrent calls. The old subprocess lifecycle was:

1. create the per-call cgroup;
2. `Popen("/bin/sh -c ...")`, whose child immediately joins the cgroup and
   execs the requested shell;
3. only then post `/started`, which synchronously compiles and attaches BPF.

In this live image step 3 takes roughly three seconds. Short commands finish
before the collector is armed, and even longer commands lose their root shell
and first executable boundary. Expanding cgroup discovery cannot recover
events that were never observed.

The subprocess launcher now starts a lightweight shell wrapper in the final
cgroup. The wrapper blocks on a dedicated inherited pipe, while preserving the
payload's stdin. The launcher posts `/started`; only after that request returns
with Stage-2 armed does it release the pipe, and the wrapper replaces itself
with `/bin/sh -c <payload>`. This guarantees an observable root exec without
replaying or changing the requested command. The previous gate implementation,
which blocked inside `preexec_fn`, was also replaced because POSIX `Popen`
waits for the child exec-error pipe and can deadlock when `preexec_fn` waits
for the parent.

The downloaded run also showed seven plugin warnings for scope, completion,
and telemetry requests. The container bundle still used the generic 800 ms
report timeout while healthy BPF startup/finalization can occupy the
in-container sidecar for several seconds. Its container-only report timeout is
now 10 seconds, matching the already-established host-sandbox setting.

Scope of change:

- `container-openclaw` uses subprocess mode and receives the pre-exec gate.
- `host-openclaw-sandbox` explicitly uses `CLAW_LAUNCH_MODE=fork-exec`, so its
  maintained launch path is unchanged.
- OpenClaw core, JSON Schema contracts, and
  `services/scheduler/src/tool_resource` are unchanged.

Validation in this Windows workspace:

- `python -m pytest services\scheduler\tests\test_launcher.py -q -p
  no:cacheprovider --basetemp .pytest-tmp-payload-gate-4`: 29 passed,
  1 skipped.
- `python -m pytest -q tests --basetemp
  .pytest-tmp-final-root-tests`: 93 passed, 2 skipped.
- From `services/scheduler`, with `PYTHONPATH=src`,
  `python -m pytest -q tests --basetemp
  ..\..\.pytest-tmp-final-scheduler`: 123 passed, 1 skipped.
- `npm.cmd test` from `packages/openclaw-plugin`: all 62 tests passed.
- `python -m compileall -q swe_rebench services\scheduler\src
  services\scheduler\tests tests`: passed.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- `python -m swe_rebench.runner prepare --config
  swe_rebench\config.yaml`: passed; main scheduler source, tracked bundle, and
  generated bundle agree.
- The tracked source fingerprint matches the current 125-file source set:
  `sha256:ffbbc4b4396d55ceb511cb3ba1c5a97e9d7f871c944061b789866e336b1b269f`.
- One-task `--dry-run` commands for both `container-openclaw` and
  `host-openclaw-sandbox`: passed and selected
  `0b01001001__spectree-64`.
- The POSIX-only test that starts a real gated shell and proves the payload
  cannot execute before release is collected here but skipped on Windows; it
  runs in Linux CI.

Validation unavailable in this Windows workspace:

- An unscoped `python -m pytest -q --basetemp .pytest-tmp-final-root`
  cannot be used as a repository-wide test command: pytest simultaneously
  collects the main scheduler, its tracked bundle copy, and `tools/smoke_test.py`
  without either scheduler `src` directory on `PYTHONPATH`, producing 17
  import errors during collection. The two supported suites above both pass.
- The live Linux Docker/eBPF acceptance command cannot run because `docker` is
  unavailable:
  `sudo -E env "PATH=$PATH" "$(command -v python3)" -m swe_rebench.runner run
  --config swe_rebench/config.yaml --dataset swe_rebench/tasks.json --sample 1
  --export --runtime-mode container-openclaw`.
- The next live run must have at least one KB-eligible call with a non-empty
  command tree. `sidecar.log` should report exec events in per-call target
  cgroups, artifacts must not contain
  `reason=no_exec_images`, and `agent-stderr.txt` should contain no scheduler
  scope/completion/telemetry timeout warnings.

## 2026-07-30 Container-OpenClaw PID-Namespace Root Remap

The next downloaded live run (`23f3952ba214`) proves that the pre-exec gate
and timeout fixes are active:

- all ten managed-wrapper calls have healthy active collectors, no ring or
  argv loss, and clean lifecycle HTTP responses;
- `agent-stderr.txt` is empty, so the earlier scope/completion/telemetry
  timeout warnings are gone;
- every collector sees two to nine exec boundaries whose `cgroup_id` exactly
  equals that call's exclusive target cgroup inode.

The remaining `no_exec_images` failure was therefore post-capture identity
filtering. The launcher and sidecar share the task container's PID namespace,
so `/started` supplied a namespace-local child PID such as `3273`. Linux BPF
`bpf_get_current_pid_tgid()` records the corresponding init-namespace host
PID. The old code treated `3273` as a host PID, selected no events under that
trusted root, and then reported an empty command tree even though the cgroup
events were present.

The Stage-2 event isolator now remaps this identity only when all of the
following fail-closed evidence is available:

1. the launcher-claimed PID is absent from captured host-PID fields;
2. events already passed the exclusive cgroup and call-window filters;
3. exactly one root exec image is `/bin/sh`, `dash`, or `bash` with `-c`/`-lc`;
4. that image's argv payload exactly equals the command registered before
   execution.

The unique observed PID becomes the effective trusted root for descendant
selection and command-tree provenance. Zero or multiple exact roots do not
remap and remain invalid. Artifact provenance records both
`claimed_trusted_root_pid` and the effective `trusted_root_pid`, with
`remap_evidence: exact_registered_root_shell`.

This is a necessary, narrow change inside
`services/scheduler/src/tool_resource`: the failure occurs after correct BPF
capture in the tool-resource identity boundary and cannot be repaired in the
launcher without access to init-namespace host PIDs. JSON Schema contracts,
OpenClaw core, and the host `fork-exec` launcher path are unchanged.

Validation in this Windows workspace:

- Focused tool-resource telemetry tests: 20 passed.
- Full scheduler suite with `PYTHONPATH=src`: 126 passed, 1 skipped.
- Root runner suite: 93 passed, 2 skipped.
- The new tests prove both the unique exact-command remap and ambiguous
  fail-closed behavior, plus that remapping remains disabled without an
  explicit per-execution cgroup.
- `python -m compileall -q swe_rebench services\scheduler\src
  services\scheduler\tests tests`: passed.
- `python tools\validate_contracts.py`: all nine schema examples passed.
- `python -m swe_rebench.runner prepare --config
  swe_rebench\config.yaml`: passed; generated and tracked scheduler bundle
  sources agree.
- One-task dry-runs for `container-openclaw` and
  `host-openclaw-sandbox`: passed.
- Current 125-file bundle source fingerprint:
  `sha256:aae7c71d03e0f1a1ce095a06e37cbd9836d4088d8497c20f42b3dafd7a3b33c7`.

Validation unavailable in this Windows workspace:

- Live Linux Docker/eBPF acceptance remains unavailable because Docker and
  the host kernel tracing environment are absent. Re-run the same
  `container-openclaw` command on Linux.
- Success requires at least one artifact with
  `event_isolation.mode: trusted_execution_root_pid_namespace_remap`, a
  non-empty successful `command_tree`, clauses present, and no
  `reason=no_exec_images`. The diagnostic log now distinguishes
  `pid_matched` from `cgroup_matched`; this environment is expected to show
  `pid_matched=0` but `cgroup_matched>0` before the explicit namespace remap.

## 2026-07-30 Container-OpenClaw Mvdan Provisioning and Final Audit

The latest downloaded run (task container `b98dab61a8c3`) proves that the
pre-exec gate, exact cgroup filtering, and PID-namespace root remap are now
working. All 12 calls reached `/started`, all 12 collectors saw two to eight
exec boundaries in the exact per-call cgroup, and every diagnostic reported
`pid_matched=0` with `cgroup_matched>0`, as expected before the explicit
namespace remap.

All 12 artifacts then failed at the next, previously unreachable stage:

```text
MvdanClientError: mvdan adapter is missing at
/root/.cache/agent-sched-bench/mvdan-clause-adapter-protocol-3-mvdan-v3.13.1
```

This was a delivery/bootstrap omission. Every task container has a clean root
cache, while setup never invoked the bundled deterministic builder. The
builder itself is intentionally tracked as mode 0644, so the existing direct
execution path would also have failed once called.

The container delivery now:

- invokes the pinned builder through `/bin/sh`, preserving its Go 1.26.1,
  mvdan v3.13.1, protocol-v3, and archive-SHA256 pins;
- builds and performs a real adapter protocol handshake during setup;
- writes an atomic, versioned setup marker only after that handshake, and
  revalidates the adapter before accepting the marker on a later setup call;
- writes a separate atomic provisioning-status JSON on both success and
  failure;
- performs an independent, read-only mvdan handshake in
  `tool_resource_preflight.json` and includes it in `stage2_ready`;
- keeps the default optional container path fail-open for the agent when
  Stage-2 is unavailable, while `stage2_required=true` now fails before the
  sidecar or agent starts and preserves the complete preflight in
  `result_summary.json`.

Post-capture parser/bridge failures are now call-granular analysis failures.
They no longer disable a healthy eBPF collector or replace real kprobe/loss
counters with zero. The runner reports these separately from semantic
rejections and fails closed in required mode. Its lifecycle audit also accepts
the new PID-namespace remap mode only when all exact-root evidence is present.

The one missing `/exited` event in the 12-call run was a separate bounded
concurrency race. The second concurrent cold `/started` synchronously occupied
the single sidecar event loop while the first short command tried to report
exit. Container/subprocess exit delivery now uses a fast 0.75-second attempt
followed by one bounded 10-second cold-start attempt. Normal calls retain the
fast path; a dead sidecar still has a finite upper bound. The host fork-exec
path retains its original three short 0.75-second attempts.

The maintained `host-openclaw-sandbox` fork-exec launch path is unchanged.
Changes under `services/scheduler/src/tool_resource` are limited to the
necessary builder invocation and collector/analyzer health classification.
OpenClaw core and public JSON Schema contracts are unchanged.

Validation in this Windows workspace:

- Main scheduler suite: 134 passed, 1 skipped.
- Root runner suite (`python -m pytest tests -q`): 94 passed, 2 skipped.
- Focused mvdan, telemetry, and launcher suite: 57 passed, 1 skipped.
- Plugin suite through `npm.cmd test`: all 62 tests passed.
- `python -m compileall -q swe_rebench services\scheduler\src
  services\scheduler\tests tests`: passed.
- `python tools\validate_contracts.py`: all nine public schema examples
  passed.
- `python -m swe_rebench.prepare --config swe_rebench/config.yaml`: passed;
  the generated bundle contains the updated scheduler, setup, and entrypoint.
- Generated setup/entrypoint text matches the tracked delivery templates, all
  paired scheduler/bundle source files match, and
  `bundle_needs_rebuild(...)` is false immediately after preparation.
- The current fingerprint covers 126 source files:
  `sha256:7a547a8eafa541c671d6f340c68ac19258347bbb1931b1b42887b7bac942ccf5`.
- One-task dry-runs for both `container-openclaw` and
  `host-openclaw-sandbox` passed and selected
  `0b01001001__spectree-64`.

Validation commands unavailable or unsuitable in this Windows workspace:

- `bash -n swe_rebench/bundle/setup.sh` and the corresponding entrypoint
  check could not start WSL (`Bash/Service/CreateInstance/E_ACCESSDENIED`);
  the sandbox-external retry was unavailable. Linux CI/live execution must
  perform these two read-only syntax checks.
- `python -m ruff check swe_rebench services\scheduler\src
  services\scheduler\tests tests` could not run because `ruff` is not
  installed. Compileall and both supported Python suites passed instead.
- Bare `npm test` is blocked by the local PowerShell execution policy for
  `npm.ps1`; the equivalent `npm.cmd test` passed all 62 tests.
- An unscoped root `python -m pytest -q` is not a supported aggregate command:
  it collects the source scheduler, the tracked delivery copy, and
  `tools/smoke_test.py` without a scheduler `src` on `PYTHONPATH`. The
  supported root and scheduler suites above both pass.
- Running pytest directly inside `swe_rebench/bundle/scheduler` produced 131
  passes, 1 skip, and three fixture-path failures because that tracked
  delivery copy intentionally has no sibling `contracts` or
  `traces/tool-resource` tree. The identical main scheduler sources pass their
  complete 133-test suite in the repository layout.
- A real clean-cache mvdan download/build/handshake and Linux Docker/eBPF run
  cannot execute here because this host lacks the Linux container/kernel
  environment. The next live run must show
  `tool_resource_preflight.mvdan_adapter.provision_status.ok=true`,
  `tool_resource_preflight.mvdan_adapter.ok=true`,
  `stage2_ready=true`, equal `/started` and `/exited` counts, no
  `analysis_failure`, and at least one healthy artifact with a non-empty
  command tree and clauses.

## 2026-08-01 openEuler BCC and Linux 6.2+ RSS Compatibility

The host Stage-2 collector now accepts both upstream Python binding names:
Debian-family `bcc` and openEuler `bpfcc`. The selected module and path are
recorded in preflight diagnostics, so a Conda interpreter no longer needs a
repository-local compatibility symlink. Host preflight also preserves BCC/
clang stderr when its Python subprocess emits JSON on stdout.

The embedded BPF source now supports both `mm_struct::rss_stat` layouts:

- Linux before 6.2 uses `rss_stat.count[index].counter`;
- Linux 6.2 and later uses `rss_stat[index].count` from
  `struct percpu_counter`.

For the per-CPU layout, each file/anonymous/shared-memory member is clamped
before summation, matching Linux 6.6's fast `get_mm_rss()` approximation. This
does not include unbatched per-CPU residuals and is therefore explicitly
reported as `percpu_counter_global_approximation` with `counter_exact=false`
in RSS provenance; that provenance is now carried into per-image clause
diagnostics. No public JSON Schema field was changed.

Validation in this Windows workspace:

- Focused telemetry and runner suite: 104 passed, 2 skipped.
- Main scheduler suite: 137 passed, 1 skipped.
- Root runner suite: 95 passed, 2 skipped.
- `python -m compileall -q swe_rebench services/scheduler/src
  services/scheduler/tests tests`: passed.
- Generated and tracked delivery copies match; the 126-file source
  fingerprint is
  `sha256:1c88599519f3fae7ff828f3d15ca8c7d37d3d7008350c4056b95767420f0d068`.
- `git diff --check`: passed.

Validation commands unavailable or requiring an adjusted temp path:

- Pytest's configured default `C:\Users\29068\.pytest-tmp` and attempted
  `C:\tmp\clawtune-pytest-*-rss` bases are not writable in this sandbox.
  The maintained suites passed with explicit repository-local `--basetemp`
  directories.
- A `--basetemp .pytest_cache\rss-final` retry is unsuitable for the two
  bundle-staleness tests because `.pytest_cache` is intentionally excluded
  from source fingerprinting; those tests passed when rerun from an ordinary
  repository-local temp directory.
- A real `BPF(text=BPF_PROGRAM)` compile/load cannot run on this Windows host.
  The next openEuler Linux run must repeat the full-source compile using the
  running kernel build tree, then run the Stage-2 preflight/semantic smoke.
## 2026-07-31 Kunpeng/ARM64 Architecture Compatibility Audit

### Status: ARM-aware / Kunpeng-ready in design, not yet certified on Kunpeng

The codebase has been reviewed for Kunpeng aarch64 compatibility.
Key findings and fixes applied:

### Compatibility Foundation (already present)

- `services/scheduler`: Python/FastAPI service with psutil, numpy, uvicorn
  dependencies — all have aarch64 wheels or build from source.
- `packages/openclaw-plugin`: TypeScript plugin, no native npm dependencies,
  CPU-architecture neutral.
- `services/scheduler/src/tool_resource/_mvdan_adapter/build.sh`: Recognizes
  `Linux:aarch64` and `Linux:arm64`, downloads `linux-arm64` Go toolchain.
- `swe_rebench`: `docker.platform` / `SWE_REBENCH_DOCKER_PLATFORM` supported
  throughout the runner, host sandbox, pre-pull, and cleanup paths.
- `docs/arm-qemu.md` + `scripts/setup/arm_qemu_setup.sh`: Full QEMU/binfmt
  path for running amd64 SWE-Rebench images on ARM hosts.
- `swe_rebench/bundle/setup.sh`: Node.js arch detection handles `aarch64|arm64`.
- `services/scheduler/src/tool_resource/telemetry.py`: eBPF syscall symbols
  include `__arm64_sys_` prefix; `CONFIG_ARCH_HAS_SYSCALL_WRAPPER` handling
  is correct for both x86_64 and arm64.

### Fixes Applied (2026-07-31)

1. **`docker-compose.yml`**: Added platform comments for ARM hosts. The
   `python:3.12-slim` base image is multi-arch; native builds resolve
   automatically. Force with `platform: linux/arm64` if needed.
2. **`services/scheduler/Dockerfile`** and
   **`swe_rebench/bundle/scheduler/Dockerfile`**: Added multi-arch
   documentation comments.
3. **`swe_rebench/Dockerfile.runtime`**: Added ARM build instructions and
   multi-arch documentation. `debian:bookworm-slim` + NodeSource + OpenClaw
   CLI must be verified on arm64.
4. **`scripts/setup/arm_qemu_setup.sh`**: Improved `ensure_binfmt_misc()` to
   check `/proc/filesystems` before `modprobe`, handling Kunpeng kernels where
   binfmt_misc is built-in rather than a module.
5. **`swe_rebench/bundle/setup.sh`**: Added host arch logging at startup for
   better diagnostics on Kunpeng.

### Remaining Verification Required on Real Kunpeng/aarch64 Linux

These commands must be run on a Kunpeng host and cannot execute in this
Windows workspace:

```bash
# ── Core validation ──────────────────────────────────────────
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root
cd packages/openclaw-plugin && npm test && npm run typecheck
cd services/scheduler && python -m pytest tests -q --basetemp ../../.pytest-tmp-scheduler

# ── Docker image build ───────────────────────────────────────
docker build --platform linux/arm64 -t agent-scheduler services/scheduler
docker build --platform linux/arm64 -t claw-runtime -f swe_rebench/Dockerfile.runtime .

# ── mvdan adapter build on arm64 ─────────────────────────────
bash services/scheduler/src/tool_resource/_mvdan_adapter/build.sh
# Verify: the built binary must be an ELF aarch64 executable.

# ── QEMU/binfmt smoke test (for amd64 task images) ───────────
sudo bash scripts/setup/arm_qemu_setup.sh install
sudo bash scripts/setup/arm_qemu_setup.sh check
export SWE_REBENCH_DOCKER_PLATFORM=linux/amd64

# ── End-to-end host-sandbox run ──────────────────────────────
sudo -E env "PATH=$PATH" "$(command -v python3)" \
  -m swe_rebench.runner run \
  --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export \
  --runtime-mode host-openclaw-sandbox
```

### Known Risks on Kunpeng

| Risk | Mitigation |
|------|-----------|
| SWE-Rebench task images are amd64-only | Use QEMU/binfmt path (`SWE_REBENCH_DOCKER_PLATFORM=linux/amd64`) |
| OpenClaw CLI npm package may lack arm64 binary | Verify `npm install -g openclaw@2026.7.1` on arm64; fall back to source build |
| BCC/eBPF kernel headers on EulerOS/openEuler | Strict preflight fails closed; kernel-devel/BCC package names differ from Debian |
| eBPF hardware counters (PERF_COUNT_HW_CPU_CYCLES) | Kunpeng 920 PMU differs from x86; perf_event_open may need `PERF_COUNT_HW_CPU_CYCLES` → ARMv8 PMU cycle counter mapping |
| cgroup v2 layout on Kunpeng | `/sys/fs/cgroup` is the standard path; cgroup v2 is available on Linux 4.15+ (Kunpeng runs 4.19+ or 5.10+) |
| `binfmt_misc` built into kernel vs. module | Handled by updated `arm_qemu_setup.sh` checking `/proc/filesystems` |

### Not Yet Validated

- `swe_rebench/bundle/setup.sh` BCC package names on EulerOS (`bcc` +
  `python3-bcc` from EPEL) — the script already has yum/dnf fallbacks with
  `|| true`.
- `npm install -g openclaw@2026.7.1` on linux-arm64 — must confirm the
  package publishes an arm64 binary or the npm registry has a fallback.
- Live eBPF kprobe attachment on Kunpeng 5.10+ kernels — the syscall symbol
  table includes `__arm64_sys_*`; walk-through verification is needed.
- `docker run --platform linux/amd64` QEMU smoke test on Kunpeng with the
  updated binfmt setup script.

### Follow-up Fixes After Review

- Corrected `scripts/setup/arm_qemu_setup.sh` to detect `binfmt_misc` by the
  last field in `/proc/filesystems`, covering both `binfmt_misc` and
  `nodev\tbinfmt_misc` formats on x86_64 and Kunpeng/aarch64 kernels.
- Moved the commented `platform: linux/arm64` hint in `docker-compose.yml` to
  the service level so uncommenting it affects the scheduler service platform
  instead of suggesting a non-portable `build.platform` placement.
- Corrected the `swe_rebench/Dockerfile.runtime` example build path.

Validation commands unavailable or unsuitable in this Windows workspace:

- `bash -n scripts/setup/arm_qemu_setup.sh` and
  `bash -n swe_rebench/bundle/setup.sh` could not run because `bash` is not
  installed in this PowerShell environment.
- Real Kunpeng/aarch64 Docker build, QEMU/binfmt smoke, and eBPF/tracefs
  attachment checks still require a Linux Kunpeng host.

## 2026-08-01 eBPF-First Defaults and Operator Documentation

The supported user path now treats strict Stage-2 eBPF telemetry as the
project default instead of presenting process/cgroup polling as an equivalent
quick start:

- `.env.example` keeps
  `AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=true`;
- the public SWE-Rebench configuration defaults to
  `host-openclaw-sandbox`, required Stage-2, and required cgroup isolation;
- `tools/check_stage2.py` provides one fail-closed host check that verifies the
  selected Python/BCC binding, kernel headers, Docker, full BPF module load,
  probe/perf attachment, cgroup creation, a real exec boundary, argv capture,
  drained lifecycle maps, and telemetry loss;
- plugin sidecar auto-start is opt-in because an unprivileged Node child cannot
  reproduce the verified root/system-Python/BCC environment;
- `tool_resource` public SDK exports are lazy, so importing the independent
  telemetry module no longer eagerly imports NumPy-backed predictor modules;
- root `prepare` restores generated plugin `dist` ownership to the original
  sudo caller, preventing later normal-user TypeScript builds from failing
  with `TS5033 ... EACCES`;
- README, deployment, sidecar, operator, SWE-Rebench, and troubleshooting
  documentation use the same eBPF-first sequence. Degraded operation is
  explicitly labelled troubleshooting-only and incomplete.

The conflict markers accidentally committed around the 2026-07-31 Kunpeng and
2026-08-01 openEuler compatibility records were removed while preserving both
records.

Validation completed in this workspace:

- Root Python suite: 100 passed, 2 skipped.
- Main Scheduler suite: 138 passed, 1 skipped.
- Focused tracked delivery telemetry suite: 26 passed.
- OpenClaw plugin suite: 64 passed; TypeScript typecheck passed.
- Contract example/schema validation and Python compileall passed.
- Local Markdown link validation and `git diff --check` passed.

Linux-only validation unavailable in this Windows workspace:

- `sudo env "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" "$STAGE2_PY"
  tools/check_stage2.py` requires the user's Linux kernel, root, BCC, cgroup
  v2, tracefs, perf events, and Docker. The user's openEuler host has already
  shown `ClawTune BPF compile OK`; it must run the new semantic preflight and
  require `stage2_ready=true` before the next accepted execution.

## 2026-08-01 Unified Setup and User Documentation

The public installation and execution path is now `scripts/clawtune.py`:

- `setup` detects `dnf` versus `apt`, chooses the system Python that owns
  `bcc`/`bpfcc`, creates one `.venv`, installs Scheduler dependencies, repairs
  narrowly scoped plugin output ownership, builds/configures the plugin, sets
  up amd64 binfmt on Kunpeng, and runs the real eBPF semantic check;
- `doctor` emits one consolidated host/interpreter/kernel/tool report;
- `doctor` also probes the fixed loopback readiness endpoint and explicitly
  reports when port 8765 is not listening. Setup states that its semantic check
  exits while normal interactive OpenClaw will auto-start the sidecar;
- setup enables plugin auto-start only with the exact verified sudo/Python/BCC
  command. The plugin's awaited `before_agent_start` gate prevents the first
  provider request until sidecar health succeeds, restoring direct
  `openclaw agent ...` without the old startup race;
- after readiness, the plugin unreferences the healthy child and capture pipes
  so they do not keep one-shot OpenClaw CLI processes alive; it retains the
  cleanup handle used by the existing shutdown path;
- `agent` restores the original automatic-sidecar user experience without
  restoring the unsafe unprivileged plugin child: it starts the verified root
  sidecar in its own process group, waits for readiness, runs `openclaw agent`,
  and stops only the sidecar it created through sudo. A pre-existing sidecar is
  reused and retained;
- `check`, `sidecar`, `agent`, and `benchmark` consistently reconstruct the verified
  privileged environment so users do not manually combine Conda, system
  Python, `PYTHONPATH`, `BCC_KERNEL_SOURCE`, and sudo;
- `tools/check_ebpf.py` presents user-facing `ready`/BCC/kernel/runtime fields
  while the historical internal preflight remains compatible;
- plugin setup recognizes a missing linked `openclaw-plugin` path left by a
  moved/renamed checkout, creates a timestamped OpenClaw config backup, runs
  `openclaw doctor --fix`, and retries the current link. It does not auto-repair
  missing paths belonging to unrelated plugins;
- the benchmark config accepts `runtime.ebpf_required`, and the Scheduler
  accepts `AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_REQUIRED`; old internal names
  remain supported for existing installations;
- README and the getting-started, configuration, Kunpeng, deployment,
  OpenClaw, sidecar, SWE-Rebench, plugin, and troubleshooting guides now form
  one project introduction and end-to-end journey. Internal stage/schema names
  appear only in developer/history references.

Validation completed in this workspace:

- Root Python suite: 114 passed, 2 skipped.
- Scheduler suite: 140 passed, 1 skipped.
- Focused CLI/config/preflight/SWE-Rebench suite: 88 passed, 2 skipped.
- OpenClaw plugin: 64 passed; TypeScript typecheck passed.
- Direct CLI help/compile checks, local Markdown links across 14 public and
  developer documents, and `git diff --check` passed.

Validation commands unavailable or adjusted in this Windows workspace:

- `python3 scripts/clawtune.py setup`, `check`, `sidecar`, `agent`, and `benchmark`
  require Linux, sudo, the running kernel's BCC/header/tracefs/cgroup/perf
  interfaces, Docker, and (for Kunpeng) aarch64 binfmt/QEMU. Run `setup` once on
  the target Kunpeng/openEuler host; its final semantic check is the acceptance
  gate. Repeat on x86_64 for the secondary native host path.
- Direct `openclaw agent ...` privileged auto-start and the awaited
  `before_agent_start` readiness gate require the target Linux OpenClaw runtime
  and controlling terminal. TypeScript build/tests validate the plugin, but the
  Kunpeng host must rerun `setup --skip-qemu` and execute one real agent turn.
- `python -m pytest services/scheduler/tests/test_config.py -q --basetemp
  C:\tmp\clawtune-docs-scheduler` could not create that sandbox path due to
  Windows permissions. The same test passed using repository-local
  `.pytest-tmp-scheduler-docs`.
- PowerShell blocked the `npm.ps1` shim for the literal `npm test` command due
  to local execution policy. `npm.cmd test` and `npm.cmd run typecheck` are the
  equivalent Windows entry points and both passed.
- `python -m ruff check ...` could not run because Ruff is not installed in
  this Windows interpreter or the repository `.venv`. Python compilation,
  tests, `git diff --check`, and a 100-column scan of the new Python files
  passed; run Ruff from the Linux `.venv` after `setup` installs the dev extras.

## 2026-08-02 Kunpeng/OpenClaw Compatibility Closure

The openEuler 24.03 aarch64 field log was reviewed against the current source.
The supported path now addresses each observed failure without persisting a
machine-specific sidecar command:

- benchmark elevation preserves a narrow, named environment allow-list, so an
  exported `LLM_API_KEY`, Docker context/proxy settings, locale, and CA settings
  survive `sudo` without secret values appearing in process argv;
- OpenClaw's removed `agents.defaults.sandbox.docker.platform` key is no
  longer generated. `DOCKER_DEFAULT_PLATFORM` is scoped to OpenClaw's Docker
  CLI environment, while runner-owned Docker calls keep explicit platform
  arguments and an explicit `SWE_REBENCH_DOCKER_PLATFORM` remains authoritative;
- sidecar launch is single-flight across duplicate plugin module loads in one
  OpenClaw process, cleanup is idempotent, and a competing process winning the
  endpoint race is accepted only after the endpoint returns the ClawTune health
  service/schema identity;
- the plugin derives the checkout, `.venv`, `.env`, running-kernel build tree,
  and direct sudo argv at runtime. Setup stores no generated absolute sidecar
  command. The managed launcher remains an intentionally absolute, setup-
  refreshed security boundary so it cannot be replaced through `PATH`;
- ARM and x86 syscall probe candidates are ordered for the detected host,
  eliminating expected wrong-architecture attach warnings. The two known
  `mm_struct::rss_stat` layouts are selected from the active kernel-header type
  instead of a kernel version threshold, covering vendor backports;
- BCC discovery imports and verifies the actual API instead of accepting an
  unrelated PyPI package, `BCC_KERNEL_SOURCE` is honored, and doctor/setup now
  include cgroup v2, clang, llc, bpftool, matching headers, and the real BCC
  interpreter in their health decision;
- mvdan adapter cache names include OS and architecture, avoiding an x86 binary
  being reused from shared storage on Kunpeng; the QEMU fallback supports both
  apt and dnf/openEuler package layouts;
- legacy diagnostic scripts no longer contain a user home, Conda path, fixed
  container/PID, embedded API key, or sudo-password pipeline. The misleading
  generic systemd example was removed because its non-root/NoNewPrivileges
  model could not provide the required eBPF runtime.

The tracked SWE-Rebench delivery bundle was synchronized from the same plugin
and Scheduler sources. Its source fingerprint was regenerated by the normal
bundle preparation command.

Validation completed in this workspace:

- `python -m pytest tests -q --basetemp .pytest-tmp-root-final`: 121 passed,
  2 skipped.
- `python -m pytest services/scheduler/tests -q --basetemp
  .pytest-tmp-scheduler-final`: 144 passed, 1 skipped.
- `npm.cmd test`: 66 passed; `npm.cmd run typecheck`: passed.
- `python -m swe_rebench.prepare --config swe_rebench/config.example.yaml
  --repo-root .`: bundle assembled successfully, including the new plugin
  launcher tests and synchronized Scheduler compatibility sources.
- `python tools/validate_contracts.py`: all 10 examples passed, including the
  new `scheduler.health.v1` contract.
- `python -m py_compile` for every changed production Python module passed.
- source/bundle SHA-256 comparisons and `git diff --check` passed.

Validation unavailable in this Windows workspace:

- `python -m ruff check ...` cannot run because Ruff is absent from this
  Windows interpreter. Run `.venv/bin/python -m ruff check .` on Linux after
  setup installs the Scheduler development extras.
- `bash -n scripts/setup/arm_qemu_setup.sh scripts/debug_cgroup.sh
  scripts/run_debug.sh scripts/verify_fix.sh scripts/verify_fix_v2.sh
  services/scheduler/src/tool_resource/_mvdan_adapter/build.sh` cannot run:
  the only available Bash is WSL and instance creation fails with
  `Bash/Service/CreateInstance/E_ACCESSDENIED`. Run the same command on Linux.
- Real BCC compilation/attachment, cgroup/tracefs/perf access, privileged
  sidecar startup, OpenClaw schema validation, Docker binfmt registration, and
  an amd64 SWE-Rebench image require the target Linux kernel and Docker daemon.
  On the Kunpeng host, rerun `python3 scripts/clawtune.py setup`, one direct
  `openclaw agent ...` turn, and `python3 scripts/clawtune.py benchmark
  --sample 1`. Repeat setup/check on x86_64 for the secondary architecture.

## 2026-08-02 Direct-agent eBPF gate closure

The latest Kunpeng agent log completed successfully, but exposed a real
fallback defect behind the non-fatal `Failed to connect to bus` message. The
launcher now quietly skips an unavailable systemd user manager, always gates
POSIX managed-wrapper payloads, and releases them only after `/started`
returns `stored:true`. EOF, timeout, HTTP 503, malformed acknowledgements, and
systemd gate failures abort with no payload execution. The privileged sidecar
creates or verifies an exclusive per-execution cgroup from authenticated PID
identity; direct-host collection no longer trusts a client path or expands a
host/shared PID namespace into machine-wide telemetry.

Validation completed:

- focused Scheduler launcher/sidecar/predictor/telemetry tests: 137 passed,
  2 POSIX-only tests skipped on Windows;
- OpenClaw plugin tests: 67 passed; TypeScript typecheck passed;
- tracked and runtime SWE-Rebench bundles rebuilt from current sources.

Validation unavailable here: the final BCC attach and cgroup move require the
target Linux kernel. On Kunpeng, run setup again and one direct agent command;
the command must now fail with exit 125 instead of running unobserved if eBPF
cannot be armed.

Follow-up field validation found that openEuler could reject creation/migration
of the exclusive child cgroup, which made the initial strict gate reject every
shell command with exit 125. The fallback now keeps eBPF enabled and uses the
sidecar-authenticated root PID plus fork/exec lineage; it does not use the
shared session cgroup as an identity filter. Sidecar/predictor/telemetry tests
after this correction: 103 passed.

The next debug run identified the remaining attach failure precisely: the
openEuler cgroup subtree did not expose optional `cpu.max` because its CPU
controller was not enabled. Quota discovery now treats a missing or malformed
`cpu.max` as unconstrained host capacity; eBPF CPU-time collection remains
active and no host controller reconfiguration is required. Telemetry and
sidecar regression tests after this fix: 78 passed.

## 2026-08-02 privileged adapter and noisy-host regression

Kunpeng benchmark evidence confirmed two regressions introduced by recent
portability work. Commit `5934562` correctly separated amd64 and arm64 adapter
caches, but the host sidecar runs as root while setup had not populated root's
cache. Container telemetry had also been left system-wide before userspace
filtering, allowing unrelated host exec traffic to overflow the BPF ring.

The unified setup now builds and handshakes the adapter under the privileged
sidecar identity. Host-sandbox preflight repeats that check and fails before an
agent run when provisioning is impossible; runtime use also self-provisions as
a final recovery path. Container collection now filters in-kernel by known
cgroups or an isolated PID namespace, while direct-host PID-lineage collection
retains its required permissive cgroup fallback.

Validation completed: focused CLI, mvdan, telemetry, and SWE-Rebench tests
passed (`141 passed, 2 skipped`); Python compilation and `git diff --check`
passed.
Ruff could not run because it is not installed in this Windows interpreter.
Live arm64 BCC compilation/attachment, root-cache adapter build, Docker cgroup
filtering, and the interrupted benchmark's end-to-end completion require the
target Kunpeng Linux host and remain to be validated there.

Kunpeng setup then exposed that `go.dev` could return non-checksum content for
the runtime `.sha256` request. The adapter builder now embeds the SHA256 values
published for the pinned Go 1.26.1 linux-amd64 and linux-arm64 archives, tries
the Aliyun and Google China download mirrors before `go.dev`, and verifies every
download against that pin. Focused tests passed (`89 passed, 2 skipped`) and
the tracked runtime bundle was regenerated. Shell syntax validation could not
run in this Windows workspace because WSL returned
`Bash/Service/CreateInstance/E_ACCESSDENIED`; run the normal setup on Kunpeng
to validate the download, checksum, extraction, and adapter handshake.

The next Kunpeng compile caught an arm64 kernel macro collision: Linux defines
`current` as `get_current()`, while the new in-kernel cgroup filter used
`current` as a local identifier. It is now named `current_cgroup_id`, with a
source-level regression assertion. Focused telemetry/SWE-Rebench tests passed
(`116 passed, 2 skipped`) and the tracked bundle was regenerated. The actual
BCC compile/attach must be repeated by rerunning setup on the Kunpeng host.
## 2026-08-02 container cached-image pull regression

A Kunpeng `container-openclaw` run had the requested linux/amd64 task image in
the local daemon, but the Docker CLI fallback interpreted `pull_policy:
missing` as an unconditional registry pull whenever a platform was specified.
The registry timeout then aborted before container creation. Container mode
now inspects the cached image's OS and architecture and skips the pull only on
an exact match. The maintained `host-openclaw-sandbox` pull/export path is
unchanged.

Container selection/cache and host regression suites passed (`118 passed, 2
skipped` total); `git diff --check` passed. A live cached-image
`container-openclaw` run remains unavailable in this Windows workspace and
must be repeated on Kunpeng.

## 2026-08-02 container dependency setup appeared to hang

The first `container-openclaw` run reached the task container but printed only
`installing system dependencies` while a quiet `apt-get update` waited on the
container network. The container-only setup now identifies the package
manager, exposes apt/npm progress and errors, uses bounded network timeouts and
limited retries, and reports an actionable fatal error when the container
cannot reach its repositories. The architecture message now correctly says
`container arch`; an amd64 task reports `x86_64` on an arm64 host by design.
The `host-openclaw-sandbox` startup path is unchanged.

A rebuilt tracked bundle and the focused SWE-Rebench/CLI regression suites
passed (`106 passed, 2 skipped`); `git diff --check` passed.
A live first-run apt/npm installation under amd64 QEMU cannot be validated in
this Windows workspace and must be repeated on the Kunpeng host.

## 2026-08-02 fork-exec trusted-root gate hardening

The fork-exec launcher now treats its pipe as an explicit success-only gate:
the payload accepts exactly one release byte after `/started` completes, while
EOF or any other byte exits without executing. Registration and gate-write
failures close the pipe and reap the child, and all parent paths close their
pipe descriptors. Regression tests cover successful release, failed
registration, EOF, and invalid gate data.

Scheduler tests passed (`164 passed, 2 skipped`), Python compilation and
`git diff --check` passed. A real POSIX fork plus live eBPF attachment and the
end-to-end Kunpeng Stage-2 benchmark cannot run in this Windows workspace and
must be validated on the target Linux host.
An additional launcher test invocation with
`--basetemp=C:\tmp\clawtune-launcher-tests` could not run because this managed
Windows environment denied creation of that directory; the same suite passed
using the writable workspace temporary directory.
## 2026-08-03 Stale OpenClaw Plugin Path Repair

- Setup now removes a missing `openclaw-plugin` entry from
  `plugins.load.paths` after backing up the config.  When the stale
  reference is not in `plugins.load.paths` (the list is empty or has no
  matching entries), setup falls back to `openclaw doctor --fix` to
  reconcile OpenClaw's internal plugin registry, then removes any stale
  paths that doctor may have restored from a last-known-good backup
  before retrying the plugin installation.  This avoids OpenClaw
  restoring the invalid config before its doctor can repair it, while
  preserving unrelated plugin paths.
- `python -m pytest tests/test_clawtune_cli.py --basetemp ../../.pytest-tmp`
  could not run in the current Windows validation environment because the only
  discovered Python executables are Microsoft Store aliases and launching
  `python.exe` failed with `A specified logon session does not exist`.
- `git diff --check` could not run because the provided Windows workspace does
  not expose the checkout as a Git worktree.

## 2026-08-03 x86 path-fix audit

The repeated path fixes were reviewed as one change series. The audit fixed a
host/runtime path-separator mix-up in the privileged Node launcher, removed a
redundant `sudo --preserve-env=HOME` request, made stale plugin path matching
work for configs copied between Windows and POSIX, and made setup stop when
OpenClaw rejects the repaired config. Scheduler packaging metadata is again
owned only by `pyproject.toml`; the duplicated `setup.py` metadata omitted the
vendored `tool_time` package data.

Focused CLI and SWE-Rebench tests passed (`141 passed, 2 skipped`), the plugin
suite passed (`68 passed`), and `git diff --check` passed. The repository-wide
pytest invocation could not collect because this Windows environment resolves
some scheduler imports from an older user-site installation and contains two
test trees with duplicate module names. A scheduler-only invocation and two
wheel-build attempts could not start the Microsoft Store `python.exe` alias
(`A specified logon session does not exist`); the `py` launcher is not
installed. The focused Ruff invocation hit the same launcher failure. Build
the scheduler wheel and run its tests and Ruff in the project venv on the
target Linux/x86 host before release.

## 2026-08-03 Python 3.10 sidecar dependency repair

An x86 benchmark reached sidecar startup but Uvicorn failed while importing
`typing_extensions.NotRequired`. The scheduler now declares
`typing-extensions>=4.12` directly instead of relying on a transitive
dependency. The runtime container setup, prebuilt Dockerfile, import smoke
checks, and tracked delivery bundle carry the same requirement. The tracked
plugin bundle was also synchronized with the privileged launcher path fix so
the maintained sources and delivery artifact do not diverge.

Focused CLI/SWE-Rebench tests passed (`142 passed, 2 skipped`), the source
plugin suite passed (`68 passed`), and `git diff --check` passed. The tracked
bundle plugin suite could not run because that generated copy has no local
`node_modules` and therefore no `tsc`; its corresponding source plugin suite
passed after the same launcher changes. A live Python 3.10 sidecar start still
must be repeated on the reported x86 Linux host by rerunning setup and the
benchmark command.

The x86 rerun showed that the host's older pip/setuptools completed editable
installation through `setup.py develop`, producing `UNKNOWN 0.0.0` and
silently skipping both runtime dependencies and the `dev` extra. `setup.py`
now contains a complete Python 3.10 compatibility mirror of the
`pyproject.toml` metadata, including `typing-extensions` and both package-data
groups. Setup also runs a post-install import probe for every scheduler runtime
dependency, so an incomplete environment fails during setup rather than at
benchmark sidecar startup. Focused regression tests passed (`145 passed, 2
skipped`) and `git diff --check` passed. The standalone setup metadata and
Python compilation commands could not start in this Windows environment due
the intermittent Microsoft Store `python.exe` logon-session failure; metadata
is exercised directly by the passing regression test. Repeat setup and the
one-task benchmark on the reported Python 3.10 x86 host.

The next x86 rerun installed the correct distribution but pip considered
dependencies under `/home/weitian/.local` satisfied. The unprivileged setup
probe could import that user site, while the sudo benchmark sidecar could not,
so it still failed on `typing_extensions`; pydantic, psutil, and Prometheus
were exposed to the same latent failure. Venv installation and its post-install
probe now run with `PYTHONNOUSERSITE=1`, forcing missing scheduler dependencies
into `.venv` while retaining distribution BCC through system-site-packages.
All privileged Python launch paths also disable user sites to prevent root or
caller-local packages from changing runtime resolution. Focused tests passed
(`146 passed, 2 skipped`), the plugin suite passed (`68 passed`), and
`git diff --check` passed. Repeat setup and the one-task benchmark on x86.

The shared-KB predictor regression suite passed (`40 passed`) and its Python
sources compiled successfully. The focused Ruff command could not start in
this Windows sandbox after repeated attempts because the Microsoft Store
`python.exe` reported that its logon session no longer existed. The approved
full scheduler Ruff command did run and reported 16 diagnostics outside the
predictor/test pair (import ordering/unused imports and existing lazy
re-exports); resolve or classify those during the full regression pass.

## 2026-08-03 machine-wide concurrent Sidecar completion

The maintained host-sandbox benchmark now has one batch-owned Sidecar for all
concurrent cases. Every Plugin/agent process is a client only: it cannot start
or stop that Sidecar. Benchmark events carry a fixed benchmark `gateway_id`
and a unique runtime identity, while the public protocol also carries
`agent_id`, `session_id`, `run_id`, and `tool_call_id`. Canonical scope, drain,
and delete requests use the Gateway/Runtime route. Legacy routes remain for
older callers but canonical callers do not borrow legacy/global scopes.
For a 320-core deployment, `8 Gateways x 16 sessions` remains the recommended
operational topology; it is deliberately not hardcoded. Today's benchmark can
use independent `openclaw agent ...` runtimes with `--parallelism`, and future
long-lived Gateways can reuse the same protocol and Sidecar without changing
resource collection.

The Sidecar's correlation maps, execution registry, Docker scope observer,
runtime monitor, trace routing, completion/model deduplication, decision
single-flight cache, and drain accounting use composite owner tuples. A
same-named runtime, run, session, or tool call in another Gateway therefore
does not become an identity boundary by accident. Sidecar HTTP calls that are
safe to retry are idempotent, and the Plugin retries transient network,
timeout, 408, 429, and 5xx failures.

All sessions intentionally share one machine KB. Runtime and clause updates go
through one background writer, batch adjacent updates, update the in-memory
generation under a lock, persist versioned snapshots atomically, and expose a
flush barrier. Runtime drain returns `drained: false` on either a timeout or a
real persistence failure. Parallel container-mode observations are merged at
the batch barrier rather than allowing last-writer-wins snapshot replacement.

CPU capacity is derived from online CPUs intersected with process affinity and
the effective cgroup cpuset, then capped by the tightest ancestor `cpu.max`.
The default housekeeping reserve is 5%, always leaving tool capacity on small
machines, with explicit reserve and budget overrides. Thus a 320-core machine
reports 16 reserved and 304 tool cores, while smaller x86 or Kunpeng machines
scale down automatically. Concurrency leases reserve predicted CPU p90 in
millicores, cap oversized jobs at the whole available budget so they can run
alone, and remain lifecycle-bound until completion, launcher exit, or exact
runtime teardown. Placement remains advisory and is never sent to the
launcher as an enforceable decision.

Stage-2 eBPF now uses one process-wide BPF program, one set of syscall/bprm
kprobes, one CPU-clock perf attachment, one ring callback, and one poller.
Dynamic cgroup and PID-namespace allow maps are reference counted. Kernel
events are fanned out to per-scope buffers; releasing one execution clears its
buffer immediately without affecting overlapping scopes. The former 8192-item
userspace exec-sequence queue was replaced by an atomic 64-bit BPF sequence.
Loss counters are measured relative to each collector lease, poll/map failures
fail closed, lifecycle methods are serialized, noisy per-call diagnostics were
removed, and the container cgroup cache is locked and bounded. SDK run IDs and
artifact reservations are also locked, preventing concurrent check-then-act
collisions.

Architecture review found no x86-only CPU-count, NUMA, or fixed L3 placement
assumption in the maintained source. Syscall probe candidates are ordered for
the detected `x86_64` or `aarch64` host, the highest unified/data cache level
is used as LLC (including Kunpeng layouts where this can be L2), and the mvdan
adapter cache/checksum selection remains architecture-qualified. On Kunpeng,
the Sidecar and eBPF stay native arm64; only official amd64 SWE-Rebench task
userspace uses QEMU. Strict Stage-2 rejects the unsafe configuration that
would run an amd64 Sidecar against an arm64 kernel.

Validation completed in this workspace:

- source Scheduler: `210 passed, 2 skipped`;
- root integration/runner suite: `169 passed, 2 skipped`;
- focused concurrent eBPF/SDK/Predictor suite: `80 passed`;
- focused SWE-Rebench runner suites: `132 passed, 2 skipped`;
- OpenClaw Plugin build/tests: `68 passed`;
- JSON Schema examples: all validated;
- full Scheduler Ruff and focused runner Ruff: passed;
- source-to-tracked-bundle hashes for Scheduler `src/tests` and Plugin
  `src/test`: zero mismatches.

The tracked bundle Scheduler test command reached `205 passed, 2 skipped` but
five repository-layout tests could not run from the nested bundle directory:
they intentionally resolve shipped KB snapshots and JSON Schemas relative to
the repository root and therefore looked under `swe_rebench/traces` and
`swe_rebench/contracts`. The identical source Scheduler suite passed from its
supported location. `mypy.exe .` exited with status 1 and no diagnostic output
in this managed Microsoft Store Python environment, so it was not usable as a
validation result.

Live BCC compilation/attachment, cgroup map updates, native arm64 syscall
attachment, amd64 QEMU task execution, and the original one-task benchmark
cannot run on this Windows host. Acceptance on Linux remains:
`python3 scripts/clawtune.py setup`, then
`python3 scripts/clawtune.py benchmark --sample 1`, followed by a multi-case
run such as `--sample 128 --parallelism 128` sized to the actual machine.

Final Windows regression also exposed and fixed a parallel first-use race in
the per-task trace directory guard. One worker could resolve a not-yet-created
root as a drive path while another resolved the newly-created root with the
Windows extended-path prefix, causing a valid sibling task to be rejected as
outside the trace root. The shared root is now created before either path is
resolved, with a 16-worker regression test. Final validation after this fix:

- Scheduler: `python -m pytest --basetemp ..\..\.pytest-tmp-final -q` from
  `services/scheduler`: `210 passed, 2 skipped`;
- framework: `python -m pytest tests --basetemp .pytest-tmp-root-final-2 -q`:
  `170 passed, 2 skipped`;
- Plugin: `npm.cmd test`: `68 passed`;
- JSON Schema examples: `python tools\validate_contracts.py`: all validated;
- source/bundle hash parity for Scheduler `src/tests` and Plugin `src/test`:
  no mismatches;
- `git diff --check`: passed.

Validation commands that could not run as written in this environment:

- bare `npm test` is blocked by the machine's PowerShell policy loading
  `npm.ps1`; the equivalent `npm.cmd test` completed successfully;
- bare repository-root `python -m pytest` is not a supported aggregate entry:
  it collects both the source and bundled Scheduler suites under identical
  module names and imports an older globally installed `agent_scheduler`.
  The independently supported source Scheduler and root `tests/` suites above
  both completed successfully.

## KV-TTL cost proxy integration (2026-08-04)

Added `tool_resource/kv_ttl.py` → `evaluate_bucket_ttl()` integration into the
tool-resource prediction output.  The prediction payload now carries a
`kv_ttl_cost` section computed at prediction time against the conservative p90
duration estimate.

### Configuration

- `tool_resource_ttl_by_bucket_s: tuple[float, ...] | None` — per-bucket KV TTL
  in seconds.  When `None`, defaults to the bucket upper bound derived from
  `tool_resource_latency_buckets_ms / 1000`.  Env:
  `AGENT_SCHEDULER_TOOL_RESOURCE_TTL_BY_BUCKET_S` (comma-separated floats).
- `tool_resource_miss_penalty_s: float | None` — optional miss penalty added to
  proxy cost.  Env: `AGENT_SCHEDULER_TOOL_RESOURCE_MISS_PENALTY_S`.

### Schema

`contracts/tool-decision.schema.json` updated: `tool_resource` now accepts an
optional `kv_ttl_cost` property (null or a `kvTTLCost` $def with the full
evaluation or an unavailable_reason fallback).

### Validation commands

```powershell
# Run the kv_ttl module tests (standalone, no infra)
$env:PYTHONPATH = "services/scheduler/src"
python -m pytest services/scheduler/tests/test_kv_ttl.py -q

# Run the config parsing tests including new env vars
python -m pytest services/scheduler/tests/test_config.py -q

# Run the prediction integration tests (kv_ttl + existing sidecar)
python -m pytest services/scheduler/tests/test_tool_resource_predictor.py -q `
  -k "kv_ttl or sidecar_response_includes or sidecar_uses_tool_resource_predictor"

# Validate the updated schema
python tools/validate_contracts.py
```
