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

- `python tools/validate_contracts.py`: all 9 contract examples passed.
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
  swe_rebench/config.yaml --dataset swe_rebench/tasks.json --sample 1 --export
  --runtime-mode host-openclaw-sandbox`. It requires the Linux/root Docker/eBPF
  host-sandbox environment used by the benchmark runner.
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

## 2026-07-29 Run Audit (Round 2): Marker Backend Switch

After the initial fixes, a second `host-openclaw-sandbox` run showed that
exec commands STILL produced no output. The `stdout=PIPE` approach in the
launcher actually made things worse by preventing stdout from flowing to
Docker exec's capture mechanism.  Analysis of the process names confirms
OpenClaw uses detached Docker exec instances whose stdout is never captured
by the Docker API — the only reliable way to forward output is through the
sidecar API, which would require significant plugin changes.

### Root Cause of Exec Output Failure

OpenClaw's Docker sandbox starts exec instances in detached mode (Docker API
`ExecStart` with `Detach: true`).  Detached execs discard stdout/stderr;
OpenClaw tracks process status via `ExecInspect` but never retrieves output.
No amount of launcher-side stdout forwarding can fix this — the Docker daemon
simply does not capture stdout for detached execs.

This is a fundamental limitation of the managed-wrapper approach in
`host-openclaw-sandbox` mode: the launcher wrapper replaces the actual
command, but Docker exec never captures the wrapper's (or its child's) stdout.

### Fix: Switch to Marker Backend

Changed `executionBackend` from `"managed-wrapper"` to `"marker"` in
`host_sandbox.py`.  In marker mode:

- The plugin registers the execution with the sidecar (timing + env vars)
  but does **NOT** replace the command.
- The original command runs directly through OpenClaw's Docker sandbox.
- OpenClaw handles stdout capture normally for direct exec commands.
- Cgroup resource monitoring continues via the shared sandbox scope.
- Clause-level telemetry (per-command exec events) is attributed via
  timing correlation instead of execution IDs.

**Trade-off**: Without the launcher, clause telemetry events cannot be
attributed to specific execution IDs.  They are still captured by the
eBPF probe and filtered by cgroup + timing window.  The `RuntimeToolResourceKB`
predictor (continuous p90 latency/CPU/memory) works with timing-based
attribution.  The `ClauseResourceKB` predictor (per-clause latency buckets)
requires execution IDs and is degraded in marker mode.

### Cgroup Discovery Fix: /proc Scanning

The diagnostic `[telemetry:diag]` consistently shows `matched=0` because
exec_boundary eBPF events have `pid_namespace_inode = 0` (the BPF probe
cannot reliably read `nsproxy->pid_ns_for_children` at exec return time).
Directory-based cgroup scanning also fails because Docker exec transient
scopes may be in unrelated parts of the cgroup tree.

**Added** `_discover_cgroup_inodes_from_proc(init_pid)` in `telemetry.py`:
- Enumerates all PIDs in `/proc` that share the container init's PID namespace.
- Reads `/proc/<pid>/cgroup` for each to find the actual cgroup path.
- Collects unique cgroup inodes from all container processes.
- Called in `ClauseTelemetryCollector.__init__` to supplement directory-based
  discovery.

This approach is more reliable because it finds cgroups where processes
ACTUALLY run, regardless of naming conventions or tree layout.

### Reverted Changes

- Reverted `stdout=PIPE` in `_spawn_shell` and `_spawn_shell_gated` — the pipe
  approach prevented stdout from reaching Docker exec's capture.
- Removed `_collect_and_forward_output` and `threading` import from launcher.
- Launcher is back to the original inherited-stdout behavior.

### Test Results

- launcher tests: 22 passed
- telemetry + sidecar + predictor tests: 68 passed
- top-level tests: 73 passed, 2 skipped (POSIX-only)
- **Total: 163 tests passed**

### Next End-to-End Validation

```bash
sudo -E env "PATH=$PATH" "$(command -v python3)" \
  -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --prepare --dataset swe_rebench/tasks.json --sample 1 --export \
  --runtime-mode host-openclaw-sandbox
```

Expected improvements over the previous run:
1. exec commands produce output (marker backend, no command wrapping)
2. cgroup_inodes includes Docker exec transient cgroups (/proc scanning)
3. matched > 0 for some exec calls (if cgroup discovery works)
4. tool_resource artifacts become healthy for matched calls

Known limitations with marker backend:
1. `launcher_tool_resource_eligible_span_ends` will be 0 (no launcher claims)
2. `tool_resource_prediction_available_ratio` may remain low
3. ClauseResourceKB predictions unavailable without execution IDs

## Not Run Locally

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
