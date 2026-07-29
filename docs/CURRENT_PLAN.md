# Current Plan

Current objective: keep this repository easy to run as an OpenClaw plugin,
sidecar, and SWE-Rebench batch runner.

## User Commands

Normal OpenClaw:

```bash
python -m pip install -e "services/scheduler[dev]"
cd packages/openclaw-plugin && npm install && npm run build && cd ../..
cp .env.example .env
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

SWE-Rebench:

```bash
cp swe_rebench/config.example.yaml swe_rebench/config.yaml
python -m swe_rebench.runner prepare --config swe_rebench/config.yaml
python -m swe_rebench.discover --sample 20 --out swe_rebench/tasks.json
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json --sample 10 --export
```

Complete host-sandbox eBPF/cgroup telemetry:

```bash
sudo -E env "PATH=$PATH" "$(command -v python3)" \
  -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --runtime-mode host-openclaw-sandbox \
  --dataset swe_rebench/tasks.json --sample 1 --export
```

For broad Docker compatibility, leave `docker.cgroup_required: false` unless a
container probe confirms `/sys/fs/cgroup/claw` can be created. With the default
false value, cgroup sampling is best-effort and can borrow the task container's
own cgroup when cgroupfs is read-only.

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
each SWE-Rebench task image through `/claw/entrypoint.sh`. It is intentionally
best-effort for Stage-2 by default. Its main failure surface is setup-heavy:
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
