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

## 2026-07-28 Experiment Audit

The `0b01001001__spectree-64` run did not reach command execution. Both LLM
spans completed with empty assistant content, OpenClaw exhausted its empty
response retry, and the trace therefore contains zero tool spans and zero
Stage-2 artifacts. Host BCC/BPF semantic preflight and sandbox cgroup discovery
were healthy. The former final error, `required resource telemetry found no
tool spans`, was a secondary symptom rather than the root failure.

The runner now records this as `agent_diagnostics.failure_kind:
empty_llm_response`, keeps the strict telemetry gate separately as
`telemetry_audit`, and marks it `not_evaluable` when no tool phase exists.
The LLM proxy accepts LF and CRLF SSE framing and surfaces successful HTTP
responses with no content/tool calls as `upstream_empty_response`; its automatic
diagnostic stores only byte counts and a response digest.

For subsequent command-bearing runs, a launcher PID without a usable cgroup-v2
child path is no longer treated as a host PID. In host-sandbox mode the sampler
retains the discovered host-side sandbox cgroup, preventing PID-namespace
collisions from attributing an unrelated host process. Stage-2 artifact audit
also accepts fallback `exec-<uuid>.json` names instead of assuming every tool
call starts with `call_`. `host-openclaw-container` is accepted as an alias for
the canonical `host-openclaw-sandbox` mode.

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

## Not Run Locally

- Live SWE-Rebench Docker task execution requires Docker access, real task
  images, and a valid upstream LLM key/model configuration.
- `cd packages/openclaw-plugin && npm test` cannot run directly from this
  Windows PowerShell sandbox because `npm.ps1` is blocked by execution policy;
  use `npm.cmd test` instead.
- `cd swe_rebench/bundle/plugin && npm.cmd run build` cannot run directly
  because the bundled plugin intentionally has no local `node_modules`.
  It was compiled with the main plugin's `tsc` and explicit `--typeRoots`, then
  validated with `node --test test/*.test.mjs`.
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
