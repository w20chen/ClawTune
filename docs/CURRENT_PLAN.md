# Current Plan

This file summarizes supported behavior, known limitations, and checks that
cannot run in this Windows workspace. Git history and `docs/REVIEW_LOG.md`
carry change history; user instructions live in the dedicated guides.

## Current State

- The sidecar publishes `call_load.v1` tool-call predictions for duration, CPU
  time, average/peak cores and distinct-mm peak RSS. Every target exposes mean,
  p50, p90 and a configurable histogram, or an explicit unavailable reason.
  See [call-load prediction](call-load-prediction.md) for conservative composition,
  Runtime v1-to-v2 label migration, admission v3 and evaluation limitations.

ClawTune supports Kunpeng/arm64 and x86_64 Linux hosts with Docker, cgroup v2,
and BCC/eBPF:

- The OpenClaw plugin sends model, tool, and execution events to a local
  ClawTune sidecar. Placement decisions are advisory in this MVP.
- Managed `exec` calls use cgroup-v2 sampling and eBPF clause telemetry.
  SWE-Rebench fails closed when required telemetry cannot start.
- Native sandbox tools such as `read` and `edit` use Docker container/PID
  attribution and do not produce exec-clause artifacts.
- JSON Schemas in `contracts/` are the public protocol source of truth. Trace
  JSONL uses schema version 6; API event contracts use `clawtune.v1`.
- SWE-Rebench runs task images through OpenClaw, supports serial or concurrent
  cases, and shares one sidecar and one evolving KB within a batch.
- Deep Research Bench runs research tasks in a basic sandbox image. Its relaxed
  gate requires an LLM span and a resource-sampled tool span, not clause
  telemetry.
- Legacy evaluation replays the three shipped prediction KBs over an external
  trace dataset. Its current protocol is the deterministic per-repository
  observation split `static_train_test_obs_per_repo`.
- The cold-start files under `traces/tool-resource/` are exported legacy-trained
  snapshots and are validated before the sidecar loads them.

See [configuration.md](configuration.md), [trace-schema.md](trace-schema.md),
[SWE-Rebench usage](../swe_rebench/README.md), and
[legacy evaluation](legacy-eval.md) for details.

## Known Limitations

- Deep Research Bench web search depends on a usable OpenClaw provider plugin
  and key. The runner defaults to Tavily and falls back to OpenClaw provider
  auto-detection when it cannot link the provider into the isolated task home.
- CPU attribution for short-lived native sandbox tools is PID-correlated, but
  the CPU value comes from the shared sandbox cgroup.
- Network totals for a derived container-cgroup scope cover the container
  network namespace, not one PID.
- Host-OpenClaw launcher spans request a host cgroup gate whenever the sandbox
  cannot create a local per-execution cgroup. With `cgroup_required=true`, the
  payload remains gated unless the sidecar creates an exclusive execution
  cgroup with verified CPU and memory accounting; a shared Docker container
  scope is not accepted as success. Authenticated execution scope wins over a
  shared completion scope, and owned cgroups remain readable through the final
  resource snapshot before cleanup.
- Legacy traces lack causal clause timestamps and per-call memory anchors.
  The new lattice evaluation uses measured clause RSS and CPU totals; the older
  continuous call-memory evaluation remains unavailable.

## Validation

### History replay and console expansion (2026-09-10)

- Runtime and Trie startup replay now uses persisted observation multiplicities instead of unconditionally appending history. Regression coverage includes pending and absorbed records, repeated restarts, old aggregate snapshot migration, identical real executions, public priors, new history and frozen immutability. Editing `services/sidecar/src/tool_resource/runtime_kb.py` was necessary because replay identity must survive its snapshot serializer/deserializer and online absorption.
- Verbose console output now includes selected results, all supplied Runtime/Trie/Lattice call candidates, labeled histograms, evidence, context, assumptions and unavailable reasons, followed by clause/legacy diagnostics and Lattice algorithms. The TypeScript build is refreshed. Public prediction schemas and units are unchanged.
- `python -m pytest services/sidecar/tests/test_kb_history.py services/sidecar/tests/test_tool_resource_predictor.py -q -p no:cacheprovider --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-history-focused --tb=short`: 68 passed.
- `python -m pytest services/sidecar/tests -q -p no:cacheprovider --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-history-sidecar-final --tb=short`: **383 passed, 2 platform skips**. The first full run (`.pytest-tmp-history-sidecar`) exposed integer-versus-float serialization in legacy history matching; numeric key normalization fixed it.
- `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=C:/Users/29068/Desktop/ClawTune/services/sidecar/src C:/Users/29068/Desktop/ClawTune" --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-history-root-final --tb=short`: **314 passed, 2 platform skips**.
- `npm.cmd test` in `packages/clawtune-plugin`: TypeScript build and **97 tests passed**. The initial formatter test counted the phrase "CPU peak" in both headers and target rows; corrected to count target rows. Rendered console text was manually inspected with the contract example.
- `python tools/validate_contracts.py`: **11 examples passed**. `git diff --check`: passed.
- Old aggregate snapshots cannot recover exact execution identities or undo already accumulated duplicate weights. Migration uses conservative repository-measurement multiplicities; a clean rebuild from original history is needed to remove pre-existing duplicate weights. No shipped snapshots or external datasets were rewritten. Linux/eBPF end-to-end validation remains unavailable on this Windows host.

### Review fixes (2026-09-10)

- Fixed the host evaluation gate to consume canonical call predictions when present, retaining explicit per-target unavailability and legacy-only trace compatibility. Collector health and frozen/online update accounting remain enforced.
- Fixed flat cold-start loading to mark structured timeout/cancel/abort/interruption records censored and exclude their partial clause labels. Regression tests verify the exported Runtime, Trie and Lattice snapshots, completed failures, and command/output text that mentions timeout.
- `python -m pytest tests/test_cold_start.py tests/test_swe_rebench_runner_inspection.py tests/test_swe_rebench_selection.py -q -p no:cacheprovider -o "pythonpath=C:/Users/29068/Desktop/ClawTune/services/sidecar/src C:/Users/29068/Desktop/ClawTune" --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-fix-focused2 --tb=short`: 181 passed, 2 platform skips. The first focused run with `--basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-fix-focused` failed nine new fixtures because they queried `read`, whereas the shipped legacy seed contains `read_file`; the corrected fixture now exercises real compatible seed evidence.
- `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=C:/Users/29068/Desktop/ClawTune/services/sidecar/src C:/Users/29068/Desktop/ClawTune" --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-fix-root --tb=short`: **314 passed, 2 platform skips**, resolving the five review failures.
- `python -m pytest services/sidecar/tests -q -p no:cacheprovider --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-fix-sidecar --tb=short`: **376 passed, 2 platform skips**.
- `npm.cmd test` in `packages/clawtune-plugin`: TypeScript build and **95 tests passed**. `python tools/validate_contracts.py`: **11 examples passed**. `git diff --check`: passed.
- Native Linux/BCC/eBPF end-to-end validation remains unavailable on this Windows host. No external datasets, shipped seed snapshots, OpenClaw core, or `services/sidecar/src/tool_resource` implementation files were modified.

### HEAD review (2026-09-10, commit 74380d1)

- `python -m pytest services/sidecar/tests tests -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-commit-review` could not collect: pytest selected the sidecar configuration root, so the relative Python paths did not resolve to this checkout's modules. The separate commands below resolved collection.
- `python -m pytest services/sidecar/tests -q -p no:cacheprovider --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-review-sidecar`: 376 passed, 2 platform skips.
- `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=C:/Users/29068/Desktop/ClawTune/services/sidecar/src C:/Users/29068/Desktop/ClawTune" --basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-review-root`: 291 passed, 5 failed, 2 platform skips. Four failures in `test_swe_rebench_runner_inspection.py` still expect online KB updates with the new frozen default; one in `test_swe_rebench_selection.py` still expects publishing with that default. The earlier all-pass root result below does not describe this final commit.
- Focused rerun of `tests/test_swe_rebench_runner_inspection.py tests/test_swe_rebench_selection.py` with the same absolute pythonpath, `--basetemp C:/Users/29068/Desktop/ClawTune/.pytest-tmp-review-gates --tb=short`: reproduced the same 5 failures; 151 passed, 2 skipped.
- `npm.cmd test` in `packages/clawtune-plugin`: TypeScript build and all 95 tests passed.
- `python tools/validate_contracts.py`: all 11 examples passed.
- Read-only snapshot/gate reproduction: loading the shipped Runtime v1 snapshot removes legacy CPU peak nodes; an otherwise healthy frozen host-run fixture with zero KB updates fails the unchanged legacy prediction gate with `missing usable conditional_p90 values for: peak_cpu_cores`.
- Synthetic flat-loader reproduction: a 60-second timeout record (`success=false`, `error_type=timeout`, resource observation ineligible with `protocol_timeout`) still produces `CompletedCall(censored=False)` and a complete 60000-ms duration label. With `trust_call_cgroup=True`, its partial CPU total and average also enter canonical training labels.
- Native Linux/BCC/eBPF end-to-end validation remains unavailable on this Windows host; the passing unit suite does not establish live collector integration or held-out prediction accuracy. No source fixes, seed changes, or external dataset writes were made during this review.

### Call-load protocol migration

Final local validation (2026-09-09):

- With `$env:PYTHONPATH=(Resolve-Path 'services/sidecar/src').Path`, `python -m pytest services/sidecar/tests -q -p no:cacheprovider --basetemp .pytest-tmp-sidecar-call-load-verified --tb=short`: **376 passed, 2 skipped**.
- With the same `PYTHONPATH`, `python -m pytest tests -q -p no:cacheprovider --basetemp .pytest-tmp-root-call-load-complete --tb=short`: **289 passed, 2 skipped**.
- In `packages/clawtune-plugin`, `npm.cmd test`: **95 passed**; `npm.cmd run typecheck`: passed.
- `python tools/validate_contracts.py`: **11 examples passed**, including `call-load.json`.
- `python tools/evaluate_call_load.py --help` and `git diff --check`: passed.
- These checks do not establish real-workload accuracy/calibration or native Linux/eBPF correctness. No external trace datasets, shipped seed snapshots or OpenClaw core files were modified.

- `npm run typecheck` could not launch through PowerShell because `npm.ps1` is blocked by the system execution policy. Use `npm.cmd run typecheck` / `npm.cmd test`; no execution-policy change is needed.

- Initial targeted validation `python -m pytest services/sidecar/tests/test_call_load.py services/sidecar/tests/test_lattice_resources.py services/sidecar/tests/test_tool_resource_predictor.py services/sidecar/tests/test_admission_leases.py -q -p no:cacheprovider --basetemp .pytest-tmp-call-load` (absolute `PYTHONPATH=services/sidecar/src`) could not execute native parser cases: the Windows mvdan adapter and `/bin/sh` builder are unavailable; Go is not installed. Unit tests use explicit parser-response fixtures; real parser/eBPF integration requires Linux. Other failures in this run were migration assertions.
- The earlier review command `python -m pytest services/sidecar/tests/test_lattice_resources.py services/sidecar/tests/test_tool_resource_predictor.py -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-interface-review` failed collection due to module-path resolution. Setting an absolute `PYTHONPATH` allowed those checks to run.

### Reproducible in this workspace or CI

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root
python -m pytest services/sidecar/tests -q --basetemp .pytest-tmp-sidecar
cd packages/clawtune-plugin && npm test && npm run typecheck
```

The legacy evaluator also has local unit coverage:

```bash
python -m pytest tests/test_legacy_eval.py tests/test_legacy_eval_export.py -q --basetemp .pytest-tmp-root
```

### Linux host only

These commands cannot run in this Windows workspace. They require Docker,
cgroup v2, matching kernel headers, BCC/eBPF privileges, and configured model
credentials:

```bash
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py check
python3 scripts/clawtune.py benchmark --sample 1 --parallelism 1
TAVILY_API_KEY="<key>" python3 scripts/clawtune.py drb --sample 1 --parallelism 1
```

Additional host probes that also cannot run here:

```bash
# Kunpeng amd64-container support
sudo bash scripts/setup/arm_qemu_setup.sh install
sudo bash scripts/setup/arm_qemu_setup.sh check

# Per-PID BCC network accounting
BCC_KERNEL_SOURCE=/usr/src/kernels/$(uname -r) .venv/bin/python -c "import os; from clawtune_sidecar.monitoring.net_accounting import ProcessNetAccounting as A; a=A([os.stat('/proc/self/ns/net').st_ino]); print(a.available, a._attach_error)"

# NUMA sampler
.venv/bin/python -c "import time; from clawtune_sidecar.topology.linux import NumaCpuUsageSampler; s=NumaCpuUsageSampler(); time.sleep(1); print(s.sample())"
```

For SWE-Rebench, verify that preflight passes, every launcher span has
`exclusive-execution-cgroup` attribution and a distinct per-execution cgroup
path, each executed clause has healthy telemetry, native sandbox spans have
`docker-exec-pid` attribution, and the required-telemetry gate passes. For Deep
Research Bench, verify at least one LLM span and one resource-sampled tool span,
plus a passed relaxed telemetry audit. Detailed output fields are defined in
[trace-schema.md](trace-schema.md).

### Known local validation gaps

- The fork-exec to host-side cgroup gate cannot be exercised end to end in
  this Windows workspace because it requires a Linux cgroup-v2 host, Docker,
  and the privileged sidecar. Unit tests cover the gated fallback and strict
  503 path; the Linux `scripts/clawtune.py benchmark` command above remains the
  runtime validation.
- Ruff is not installed in the active Python environment. The commands and
  exact error are recorded in the incremental validation gap below; contract
  validation, full tests, typecheck, compileall, and `git diff --check` are the
  available local gates.
- The top-level pytest command requires the project virtual environment and
  `services/sidecar/src` on `PYTHONPATH` in this checkout. The verified Windows
  equivalent is
  `$env:PYTHONPATH=((Resolve-Path '.').Path + [IO.Path]::PathSeparator + (Resolve-Path 'services\\sidecar\\src').Path); .\\.venv\\Scripts\\python.exe -m pytest tests -q --basetemp .pytest-tmp-root`.
- A stale `%USERPROFILE%\.pytest-tmp` is not removable by this account. Always
  give pytest a workspace-local `--basetemp` as shown above.
- `python -m swe_rebench.prepare` successfully generates the current runtime
  scripts under `swe_rebench/.runtime/assets/`, but Git for Windows Bash cannot
  run `bash -n` in this account because its WSL service startup fails with
  `Bash/Service/CreateInstance/E_ACCESSDENIED`. CI runs both syntax checks on
  `ubuntu-latest` after generating the runtime assets.

## Two-sandbox Kubernetes delivery (2026-08-10)

The sibling `claw-k8s` repository now has an additive
`deploy/two-sandbox/` mode. Each validated tenant ID maps to one Runtime
Deployment (OpenClaw, plugin, and loopback-local sidecar) and one Tool
Deployment (non-root SSH executor). OpenClaw `2026.7.1-2` uses its built-in SSH
sandbox backend. `exec`, `process`, `read`, `write`, `edit`, and `apply_patch`
are allowed on that backend; browser and control-plane tools are denied.

This topology deliberately runs the plugin with `executionBackend=hook-only`
and disables cgroup, affinity, NUMA, and required eBPF collection in
the Runtime Pod. The sidecar cannot observe trustworthy Tool Pod PID/cgroup
scope, so existing absent/unattributed scope and unavailable telemetry behavior
is used. Tool lifecycle traces and advisory prediction remain active. No public
contract fields and no code under `services/sidecar/src/tool_resource` were
changed.

Local checks completed:

```bash
python deploy/two-sandbox/test_render.py
bash -n deploy/two-sandbox/cell.sh deploy/two-sandbox/smoke-test.sh scripts/two-sandbox/*.sh
cd packages/clawtune-plugin && npm.cmd test
OPENCLAW_CONFIG_PATH=<claw-k8s>/deploy/two-sandbox/openclaw-sandbox.example.json node <npm-openclaw>/openclaw.mjs config validate
```

The following validation commands could not run in this Windows workspace:

```bash
# kubectl is not installed and no Kubernetes context is available.
kubectl apply --dry-run=server -f <(bash deploy/two-sandbox/cell.sh render ...)
bash deploy/two-sandbox/smoke-test.sh --namespace agents --tenant tenant-a --other-tenant tenant-b

# Docker/BuildKit is not installed, so image startup and non-root sshd require Linux CI/cluster validation.
docker build -f docker/Dockerfile.runtime -t claw-runtime:test .
docker build -f docker/Dockerfile.tool-sandbox -t claw-tool:test .

# shellcheck is not installed; Git for Windows bash -n was used instead.
shellcheck deploy/two-sandbox/*.sh scripts/two-sandbox/*.sh
```

On a real Linux cluster, run the deployment guide twice (tenant-a and tenant-b),
then execute the smoke command above. Repeat once without RuntimeClass options
and once with `--runtime-class kata-fc --tool-runtime-class kata-fc`. The smoke
test verifies separate Pods/hostnames/PID namespaces/filesystems, remote tool
output and file placement, Tool credential absence, no Docker socket,
cross-tenant SSH denial, and optional managed-resource cleanup.

## Incremental bug review validation gap (2026-08-14)

The following validation command could not run in this Windows workspace:

```powershell
# Ruff is not installed in the active Python environment (`No module named ruff`).
python -m ruff check services/sidecar/src services/sidecar/tests swe_rebench deep_research_bench legacy_eval scripts tools tests
python -m ruff check --select F821,F601,PLW0127 services/sidecar/src/clawtune_sidecar/api/app.py services/sidecar/src/clawtune_sidecar/api/dependencies.py services/sidecar/tests/test_launcher.py services/sidecar/tests/test_sidecar.py
```

## Tool-VM production tracefs validation (2026-08-20)

Ubuntu 22.04 BCC 0.18 in the ClawBox production Tool image reads
`/sys/kernel/debug/tracing/events/sched/sched_process_exit/id` through libbcc.
In a real ARM64 Kata/Firecracker guest (`6.18.28`), mounting debugfs over
Kata's masked `/sys/kernel/debug` returned zero, but debugfs rejected creation
of the missing `tracing` directory with `EPERM`. A private tmpfs overlay plus a
tracefs mount at the legacy path exposed tracepoint ID 197. The native bridge
workload then produced six valid, zero-loss artifacts with cleanup `ok`; final
KB eligibility remained blocked by a ClawBox production-image packaging issue
(`numpy` absent), not by BPF compilation, loading, or attachment.

Linux-only validation commands used on the designated ClawBox test cluster:

```bash
mount -t tmpfs -o mode=0755,nosuid,nodev,noexec tmpfs /sys/kernel/debug
mkdir -p /sys/kernel/debug/tracing
mount -t tracefs tracefs /sys/kernel/debug/tracing
cat /sys/kernel/debug/tracing/events/sched/sched_process_exit/id
```


## Lattice resource extension (2026-09-09)

See [lattice-resources.md](lattice-resources.md) for metric semantics, snapshot
compatibility, the reproducible cold-start exporter, and held-out evaluation.
Lattice now emits clause CPU time, average cores, 500 ms peak cores, and sampled
RSS peak p50/p90 using independent target contexts. Heavy thresholds are not KB
labels. The shared raw snapshot is v2, with a v1-compatible reader. Old RuntimeKB
and prefix-bucket outputs remain active pending a separate consumer migration.

Cold start was regenerated from the user-supplied read-only
`D:/swe277-full-5be74da-20260726` directory. Within each repo, 80% of tasks go to
training (integer rounding; singletons train-only), seed 42. The manifest lists
239 training tasks and 38 held-out tasks. Test observations never update the KB.

Validation commands and environment limitations for this change:

- `python tools/validate_contracts.py`: passed.
- `python -m pytest services/sidecar/tests -q -p no:cacheprovider --basetemp .pytest-tmp-sidecar-resource-final`: passed; two POSIX subprocess-control tests skip on Windows.
- Initial `python -m pytest tests -q -p no:cacheprovider --basetemp .pytest-tmp-root-resources` could not collect because this checkout's sidecar modules were absent from Python's import path. Resolved with `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-root-resource-final`; two POSIX permission-bit tests skip on Windows.
- Initial `npm run typecheck` in the plugin directory could not run because PowerShell blocks npm.ps1. `npm.cmd run typecheck` and `npm.cmd test` in `packages/clawtune-plugin` passed. An intermediate `npm.cmd run typecheck` from the repository root could not run because the root has no package.json; corrected by using the plugin working directory.
- The initial focused pytest run warned that the existing sidecar pytest cache directory was not writable. Subsequent runs use `-p no:cacheprovider`; test execution itself succeeded.
- `python scripts/export_resource_lattice.py --dataset D:/swe277-full-5be74da-20260726 --seed 42 --train-fraction 0.8`: produced the seed and split/source manifest.
- `python scripts/evaluate_resource_lattice.py --dataset D:/swe277-full-5be74da-20260726`: completed offline held-out evaluation; see resource-lattice-evaluation.json.
- Live Linux collector validation (`python3 scripts/clawtune.py check`) cannot run natively on this Windows host because cgroup v2/BCC/eBPF are unavailable. No new live collector accuracy claim is made; real recorded eBPF artifacts were replayed read-only instead.

The empirical p90 estimates are not calibrated scheduling guarantees: held-out
memory p90 coverage is about 81%. CPU evidence comes from 8-core-quota source
runs. Full lattice preparation still uses the existing shared-lock lifecycle;
background rebuilds can delay prediction requests at larger KB sizes.

Final verification: sidecar 340 passed / 2 platform skips; root 286 passed /
2 platform skips; plugin 95 passed. Release verification used the same pytest
commands above with basetemp `.pytest-tmp-sidecar-resource-release` and
`.pytest-tmp-root-resource-release`. Contract examples, snapshot hand-off, split
disjointness, seed/manifest/evaluation hashes, and `git diff --check` passed.
Optional Ruff availability probe (`python -m ruff --version`) found no installed
ruff module; no Ruff lint result is claimed.


## Offline lattice accuracy report (2026-09-09)

`python scripts/benchmark_lattice_accuracy.py --dataset D:/swe277-full-5be74da-20260726`
uses the existing frozen seed and its within-repository 80/20 task manifest.
It adds physical-unit errors, WAPE, within-factor-two rate, p90 coverage and
pinball loss, plus paired comparisons with a training-only repo/binary baseline.
It does not alter model logic, hyperparameters, the seed, or external datasets.
Outputs: docs/lattice-accuracy/{report.md,metrics.json,predictions.jsonl,accuracy.png}.

`python -m pytest tests/test_lattice_accuracy_metrics.py -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-accuracy`: 3 passed.
The first report run stopped at the immutable-training assertion because Python
tuple argv values were compared directly with JSON list argv values. Normalizing
both to JSON before comparison fixes the representation mismatch; no training
update occurred. The corrected report command is rerun before delivery.

The corrected offline run completed: 38 test tasks across 36 repositories,
1,387 unique command queries, zero test updates. Immutable training observations
were verified after prediction. Numerical metric tests passed; accuracy.png was
visually inspected. Results show limited predictive accuracy, not merely missing
coverage: no lattice algorithm beats the repo/binary baseline on time or CPU peak
MAE, and larger realized memory workloads remain poorly predicted. Full paired
errors, tail losses, and raw prediction pairs are retained in the report folder.

## Tool-level PMU counting (2026-09-10)

ClawTune now arms one task-inherited four-event `perf_event_open` counting group
at the existing gated Tool root. The public `pmu_profile_v1` contract preserves
raw/scaled values, enabled/running time, event semantics, and coverage quality.
Only reliable IPC, LLC MPKI, and LLC miss-rate values enter online Runtime KB
evidence. The collector is process-wide, so one budget covers all concurrent
sessions. ClawBox imports this same core into each Tool CubeSandbox VM and only
changes guest execution scope/capabilities. See [pmu-profiling.md](pmu-profiling.md).

Validation commands and environment limitations:

- `python tools/validate_contracts.py`: passed from the repository root,
  including `pmu_profile_v1`. One combined final check first invoked the same
  relative path from `services/sidecar` and failed with file-not-found; rerunning
  from the documented root working directory passed.
- `python -m pytest tests -q -p no:cacheprovider` from `services/sidecar`: 393 passed, 2 POSIX skips.
- `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ."` from the ClawTune root: 315 passed, 2 POSIX skips.
- Focused ClawBox PMU/trace/cgroup/KB/online-signature/native-artifact tests passed; one unrelated platform test skipped.
- `python tools/validate_pmu.py --require-reliable --concurrency 8 --max-active 8 --high-concurrency 64 --benchmark-count 40`: cannot run on this Windows workspace because `perf_event_open`, Linux task inheritance, and a hardware/vPMU are unavailable. It must run on production x86 Linux and Kunpeng/Cube guests; no live overhead or counter-accuracy claim is made from this host.
- `gofmt -w toolbridge/collector.go toolbridge/guest_collector.go toolbridge/main.go` and `go test ./...` in ClawBox cannot run because Go/gofmt are not installed in this workspace. The files require Linux/Go CI validation before release.
- The full ClawBox Python suite still has five pre-existing/environment failures: one Windows `/proc` snapshot path, one Windows HTTP disconnect behavior mismatch (502 versus `RemoteDisconnected`), and three assertions that still expect legacy `runtime_tool_resource_kb_v1` although the sibling ClawTune checkout already emits v2. The focused PMU paths pass and this change does not rewrite those unrelated baselines.

## Project inspection against benchmark failure log (2026-09-10)

Inspection only; no implementation fixes applied. Four local reproductions:

- SOCKS: with process-local ALL_PROXY=socks5://127.0.0.1:9, constructing the real HTTPX client raises the same missing-socksio ImportError as the supplied log. The wrapper preserves proxies, but pyproject.toml and the generated container install list specify plain httpx. Setup only imports the dependencies.
- Diagnostics: four failed, empty model spans are classified as empty_llm_response despite a proxy error in stderr. failed_llm_span_ends does not control classification.
- Authentication: TestClient GET /v1/status with the correct configured token returns 500; direct verify_bearer invocation leaves a Header object unresolved, producing AttributeError: 'Header' object has no attribute 'startswith'.
- Compression: real HTTPX MockTransport returning gzip JSON makes the non-streaming chat proxy client raise DecodingError. The response retains content-encoding after decompression and JSON reserialization.

Validation commands:

- Root: `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-review-root`: 315 passed, 2 skipped.
- Sidecar directory: `python -m pytest tests -q -p no:cacheprovider --basetemp ../../.pytest-tmp-review-sidecar`: 393 passed, 2 skipped.
- Plugin directory: `npm.cmd test`: build succeeded, 97 tests passed.
- Root: `python tools/validate_contracts.py`: all 12 contract examples passed.
- Ad hoc `python -` probes reproduced the four cases with temporary directories, TestClient, MockTransport and process-local environment patches. No external provider requests or dataset changes.
- `python3 scripts/clawtune.py benchmark --sample 1 --parallelism 1` and `python3 scripts/clawtune.py check` cannot be validated natively on this Windows host: Docker is not installed and Linux BCC/eBPF/cgroup v2 are unavailable. No successful Linux end-to-end run is claimed.
- Initial `rg --files` encountered existing inaccessible pytest cache/temp directories; subsequent reads targeted source paths. An initial rg wildcard positional path failed on Windows and was replaced by an explicit filename. The first documentation patch failed to match its context; this entry was appended instead.

## Pipeline-dependent consumer filtering (2026-09-10)

The supplied benchmark trace confirms that a downstream pipe consumer's wall
interval is normally dominated by the upstream stage: `pytest | tail` measured
25,800.7/25,803.4 ms, `pip install | tail` measured
129,768.1/129,771.2 ms, and `python ... | grep` measured
2,698.8/2,701.4 ms. These values are not independent labels for `tail` or
`grep`.

Training, historical import, online observation, lattice/trie prediction, and
call-duration composition now share one structural rule. A configured consumer
is excluded only when `in_pipe` is true and `pipeline_position` is greater than
zero. The same executable remains eligible standalone or at pipeline position
zero. The set includes `tail`, `head`, `wc`, `grep`/`egrep`/`fgrep`/`rg`, `cat`,
and the existing presentation/filter utilities. Older telemetry rows recover
these fields by matching `(bin, argv)` against the stored command parse.

The predictor now preserves parser structural fields instead of normalizing
every clause to a serial `single`. Simple pipeline duration uses the maximum of
retained stage samples, while serial groups sum. The cold-start lattice and
clause snapshots were regenerated from the read-only dataset. The aggregated
clause snapshot schema is v5 so polluted v4 snapshots are rejected rather than
silently reused. Eligible training
observations changed from 11,253 to 7,800; held-out evaluation now covers 1,092
unique commands and was regenerated together with its report and figure.

Validation:

- `python -m pytest tests -q -p no:cacheprovider --basetemp ../../.pytest-tmp-pipe-sidecar-final` from `services/sidecar`: 396 passed, 2 platform skips.
- `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-pipe-root-final2`: 317 passed, 2 platform skips.
- `python tools/validate_contracts.py`: all 12 contract examples passed.
- `python scripts/export_resource_lattice.py --dataset D:/swe277-full-5be74da-20260726 --seed 42 --train-fraction 0.8`: regenerated both cold-start snapshots and the manifest without modifying the dataset.
- `python scripts/evaluate_resource_lattice.py --dataset D:/swe277-full-5be74da-20260726`: completed with 1,092 unique queries; p95 query time 14.01 ms.
- `python scripts/benchmark_lattice_accuracy.py --dataset D:/swe277-full-5be74da-20260726`: completed with 38 held-out tasks, 36 repositories, and 1,092 unique queries; the generated chart was visually inspected.
- Native mvdan adapter execution and live eBPF collection cannot be validated on this Windows host. The exporter exercised the conservative parser fallback; Linux CI/benchmark setup must validate the native adapter path.
- `npm.cmd test` could not run at the previously documented `plugins/clawtune-srb` path because this checkout has no such directory or Node package; there is no plugin test target in this repository layout.

## Three-path demo design (2026-09-10)

The target design is recorded in [DEMO_SYSTEM_DESIGN.md](DEMO_SYSTEM_DESIGN.md).
Keep daily OpenClaw use with a persistent user KB, serial SWE-Rebench as an
online-learning user simulation with a run-owned KB, and one fixed-trace offline
train/test pipeline. Strict within-repository task-held-out evaluation belongs
to the offline path; the SWE simulation is no longer proposed as a second
held-out evaluation frontend.

The first implementation priority is separating immutable seed bundles from
daily state and per-run state. Today the daily sidecar's default writable
`traces/tool-resource` location is also the runner's seed source. The design
also consolidates split/export/evaluation implementations, retains one shared
prediction core, and defines a dependency-aware removal list for redundant
demo paths. `cold_start` currently imports `legacy_eval._bootstrap`; migrate
that dependency before deleting the legacy evaluator.

This delivery changes documentation only. Proposed CLI commands, state layouts,
online benchmark defaults, and deletion decisions are not implemented yet.
No external datasets or existing knowledge bases were modified.

Validation: inspected CLI routing, sidecar load/update/persistence logic, plugin
launch and repo identity code, current manifests, offline loaders, and import
dependencies; checked the documentation diff. Runtime tests are not applicable
to this documentation-only change. The previous review's
`python scripts/clawtune.py benchmark --help` ran but exposed only wrapper help;
`python -m cold_start --help` ran successfully. Live
`python3 scripts/clawtune.py benchmark --sample 2` and
`openclaw gateway run` / `openclaw tui --session main` cannot be validated on
this Windows workspace with Linux BCC/eBPF unavailable; they remain future
implementation acceptance checks, not claimed successful runs.

## Multi-dataset three-path implementation (2026-09-11)

The current plan and implemented boundaries are documented in
[MULTI_BENCHMARK_IMPLEMENTATION.md](MULTI_BENCHMARK_IMPLEMENTATION.md). This
supersedes the prior documentation-only/SWE-only scope and the proposed removal
of Deep Research Bench. Five adapters are peers: swe-rebench,
deep-research-bench, swe-bench-verified, bfcl, terminal-bench.

Implemented:

- One public benchmark parser and serial online run lifecycle, immutable seed
  initialization, per-task generation reporting, saved task order, and explicit
  boundary-only resume. New tasks retain the run KB but receive new agent/task
  environments. Config and seed hashes must match when resuming.
- Daily state under the invoking user's state directory, separate from trace
  outputs; managed KB writer lock, atomic three-snapshot generation commit,
  last-commit recovery, and bounded generation retention. Health reports KB
  ownership; the plugin rejects a managed owner mismatch.
- Native repository/research runtime reuse; BFCL native function schemas,
  mutable backend instances and multi-turn session; Terminal native Compose
  copy and client-container tools. External task datasets remain read-only.
- Unified offline v5/v6 import, per-dataset and per-repository/category task
  split, train-only three-layer seed, shared pre-execution prediction API,
  frozen test, target eligibility gates, coverage/error/calibration/baseline
  reports. Mixed datasets train completely separate KBs.
- Common config template, JSON Schemas for new artifacts, wheel-bundled
  canonical contracts and demo seed, updated README. Removed unused replay and
  old DRB wrapper implementations; `drb` delegates to the common benchmark CLI.
  Historical evaluators and reused host helpers remain internal/compatibility
  code rather than being deleted while other code still imports them.

Validation commands and results:

- `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-demo-root-accepted --tb=short`:
  327 passed, 2 platform skips.
- From `services/sidecar`, `python -m pytest tests -q -p no:cacheprovider --basetemp ../../.pytest-tmp-demo-sidecar-final --tb=short`:
  396 passed, 2 platform skips. Existing FastAPI lifespan deprecation warnings.
- From `packages/clawtune-plugin`, `npm.cmd test`: TypeScript build and all 98
  tests passed. This is the correct plugin test directory; it supersedes the
  earlier note about the nonexistent `plugins/clawtune-srb` test target.
- `python tools/validate_contracts.py`: all 12 existing protocol examples passed;
  new seed/state/split/run/report/bridge contracts are exercised by workflow
  tests and actual offline artifact generation.
- `python scripts/clawtune.py benchmark --list`, `benchmark --sample 1 --dry-run`,
  `benchmark --help`, and `offline --help`: successful, with the real common
  parser exposed, all five peers listed and ordered online semantics displayed.
- `python -m compileall -q benchmarks offline services/sidecar/src/clawtune_kb`:
  successful.
- `python -m pip wheel ./services/sidecar --no-deps --no-build-isolation --wheel-dir .runtime/wheels`:
  successful; extracted-wheel subprocess outside the source package successfully
  validated its bundled seed and contracts.
- `python scripts/clawtune.py offline --dataset D:/swe277-full-5be74da-20260726 --benchmark swe-rebench --rss-unit MiB --output .runtime/offline/validation-swe277`:
  successful against the read-only real trace collection. 239 train / 38 test;
  1,977 eligible test calls; test updates = 0. MAE 1,643.62 ms vs tool-median
  baseline 1,687.76 ms, WAPE 0.9705, P90 coverage 0.7699. Only duration labels
  met the v5 call attribution gate. This is a modest improvement, not a claim
  of strong CPU/memory accuracy. The exact split, exclusions, trained bundle,
  predictions and report are under the named output directory.
- Focused workflow tests verify all-five-dataset isolation, unchanged training
  snapshots after modifying only test labels, crash recovery, one-writer
  exclusion, immutable seed rejection, BFCL state across turns, Terminal
  read-only input enforcement, bridge authentication/deduplication, and a
  shared run KB with boundary-only resume. Native backend calls are mocked.

Intermediate failures were resolved: four old tests seeded the previous traces
directory; three explicit trace-import tests accidentally received the new
default seed until initialization was limited to managed startup; the legacy
setup metadata test needed to stub the new wheel build command import. One
PowerShell inline edit failed due to quoting and was reapplied with a here-string.

Validation commands that cannot run on this host:

- `openclaw gateway run` / `openclaw tui --session main`, and
  `python3 scripts/clawtune.py check`: native Linux OpenClaw + BCC/eBPF/cgroup
  acceptance cannot run on this Windows host. The installed Python Scripts
  `openclaw.exe` is not evidence of the native Node OpenClaw runtime.
- `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 2` and
  the equivalent `swe-bench-verified` invocation with `--dataset`: no native
  Linux Docker/eBPF runtime is available. Task image startup and live shared-KB
  learning therefore remain target-host acceptance checks.
- `python3 scripts/clawtune.py benchmark --benchmark deep-research-bench --sample 2`:
  same host limitation; actual native search-provider integration is unverified.
- `python3 scripts/clawtune.py benchmark --benchmark bfcl --category multi_turn_base --sample 2`:
  `bfcl_eval` is not installed, and native Linux OpenClaw is unavailable.
- `python3 scripts/clawtune.py benchmark --benchmark terminal-bench --dataset /data/terminal-bench/tasks --sample 2`:
  Docker is absent from PATH and the native task environment cannot be launched.
  Compose validation and backend tests do not count as a live harness run.
- Native mvdan/eBPF platform tests remain the two platform skips in each Python
  suite. No live benchmark, official solve score, BFCL AST-only evaluator, or
  persistent Terminal TTY support is claimed by this delivery.

## PMU correctness audit (2026-09-11)

Reviewed IPC, LLC read MPKI and LLC read miss fraction from perf ABI through
collector, execution attribution, KB persistence, and offline evaluation.
The formulas and per-event non-GROUP read format agree with the Linux
[perf_event_open manual](https://man7.org/linux/man-pages/man2/perf_event_open.2.html).

Fixed actual correctness gaps:

- Abort, signal termination, lost exit callbacks and completion fallback can
  no longer produce reliable training profiles. A failed group disable also
  makes the profile partial. Normal nonzero program exits remain distinct from
  signal/cancel censoring.
- Invalid counter timing (`running > enabled`) is rejected rather than clamped
  into apparently perfect coverage. Misses exceeding accesses yield an unknown
  miss fraction and a partial profile, never a clamped percentage.
- Learning rechecks raw counters, event semantics, full running time, coverage
  flags, collector errors and execution ID; it recomputes ratios instead of
  trusting serialized derived values. Undefined denominators remain null.
- Native perf FDs use CLOEXEC. A syscall-argument test checks the 112-byte ABI,
  inherit/enable-on-exec flags, leader/member grouping and the 24-byte read
  format compatible with inheritance.
- The offline runner now scores the three PMU targets through their separate
  runtime-KB evidence interface. They were previously omitted because they are
  deliberately outside call_load.v1. It uses frozen training evidence and the
  same target-specific baselines. Historical v5 without PMU labels is unchanged.
- The public PMU schema now requires the stronger reliable-profile invariants
  and bounds LLC miss rate to [0, 1]. The runtime KB additionally rejects an
  out-of-range miss fraction; this small `tool_resource` change is necessary to
  prevent direct CompletedCall inputs from bypassing the metric constraint.

Validation:

- `python -m pytest tests -q -p no:cacheprovider --basetemp ../../.pytest-tmp-pmu-full --tb=short`
  from `services/sidecar`: 404 passed, 2 platform skips.
- After adding the syscall ABI regression,
  `python -m pytest tests/test_pmu.py -q -p no:cacheprovider --basetemp ../../.pytest-tmp-pmu-abi --tb=short`
  from `services/sidecar`: all 19 PMU tests passed.
- `python -m pytest tests -q -p no:cacheprovider -o "pythonpath=services/sidecar/src ." --basetemp .pytest-tmp-pmu-root --tb=short`:
  328 passed, 2 platform skips, including independent frozen PMU evaluation.
- `python tools/validate_contracts.py`: all 12 protocol examples passed.
- `python tools/validate_pmu.py --require-reliable --output .runtime/pmu-validation.json`
  cannot execute hardware validation here: it returned `unsupported / Linux
  required`. No hardware result file or successful PMU acceptance is claimed.
  Run that same command with the target Linux host's privileged sidecar Python
  on both deployment CPU architectures. True counter accuracy, descendant
  inheritance and virtualized PMU behavior remain hardware acceptance items.
