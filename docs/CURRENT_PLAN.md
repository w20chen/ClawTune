# Current Plan

This file summarizes supported behavior, known limitations, and checks that
cannot run in this Windows workspace. Git history and `docs/REVIEW_LOG.md`
carry change history; user instructions live in the dedicated guides.

## Current State

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
