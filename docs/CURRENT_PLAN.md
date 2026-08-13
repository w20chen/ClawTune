# Current Plan

This file summarizes supported behavior, known limitations, and checks that
cannot run in this Windows workspace. Git history and `docs/REVIEW_LOG.md`
carry change history; user instructions live in the dedicated guides.

## Current State

ClawTune supports Kunpeng/arm64 and x86_64 Linux hosts with Docker, cgroup v2,
and BCC/eBPF:

- The OpenClaw plugin sends model, tool, and execution events to a local
  Scheduler sidecar. Placement decisions are advisory in this MVP.
- Managed `exec` calls use cgroup-v2 sampling and eBPF clause telemetry.
  SWE-Rebench fails closed when required telemetry cannot start.
- Native sandbox tools such as `read` and `edit` use Docker container/PID
  attribution and do not produce exec-clause artifacts.
- JSON Schemas in `contracts/` are the public protocol source of truth. Trace
  JSONL uses schema version 6; API event contracts use `scheduler.v1`.
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
- Benchmark launcher spans report `host_cgroup_gate=false` by design; the
  sidecar derives their host cgroup from `/proc/<host_pid>/cgroup`.
- Legacy traces lack causal timestamps and memory samples. Memory is therefore
  not evaluated, and CPU coverage is limited to eligible sampled clauses.

## Validation

### Reproducible in this workspace or CI

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root
python -m pytest services/scheduler/tests -q --basetemp .pytest-tmp-scheduler
cd packages/openclaw-plugin && npm test && npm run typecheck
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
BCC_KERNEL_SOURCE=/usr/src/kernels/$(uname -r) .venv/bin/python -c "import os; from agent_scheduler.monitoring.net_accounting import ProcessNetAccounting as A; a=A([os.stat('/proc/self/ns/net').st_ino]); print(a.available, a._attach_error)"

# NUMA sampler
.venv/bin/python -c "import time; from agent_scheduler.topology.linux import NumaCpuUsageSampler; s=NumaCpuUsageSampler(); time.sleep(1); print(s.sample())"
```

For SWE-Rebench, verify that preflight passes, launcher spans are cgroup-backed,
each executed clause has healthy telemetry, native sandbox spans have
`docker-exec-pid` attribution, and the required-telemetry gate passes. For Deep
Research Bench, verify at least one LLM span and one resource-sampled tool span,
plus a passed relaxed telemetry audit. Detailed output fields are defined in
[trace-schema.md](trace-schema.md).

### Known local validation gaps

- Ruff is not installed in the current Python environment, so `python -m ruff
  check ...` cannot run. Use contract validation, tests, and `git diff --check`.
- The documented top-level pytest command requires the project virtual
  environment and an explicit Scheduler source path in this checkout. Running
  it with the system interpreter fails during collection with
  `ModuleNotFoundError: No module named 'tool_resource'`; the equivalent check
  succeeds with
  `$env:PYTHONPATH=(Resolve-Path 'services\\scheduler\\src').Path; .\\.venv\\Scripts\\python.exe -m pytest tests -q --basetemp .pytest-tmp-root`.
- The bundled Scheduler test copy has existing repository-layout failures
  because it resolves fixtures below `swe_rebench/`; validate the source suite
  under `services/scheduler/tests` instead.
- A stale `%USERPROFILE%\.pytest-tmp` is not removable by this account. Always
  give pytest a workspace-local `--basetemp` as shown above.
- Windows ACLs currently prevent this account from removing the historical
  `swe_rebench/bundle/` tree completely (notably the generated wheel and source
  archive) and the stale `swe_rebench/.pytest-runner-tmp/` and
  `swe_rebench/.pytest-scheduler-tmp/` directories. These are generated or
  obsolete outputs, not runtime inputs; remove them from an elevated host shell.

## Two-sandbox Kubernetes delivery (2026-08-10)

The sibling `claw-k8s` repository now has an additive
`deploy/two-sandbox/` mode. Each validated tenant ID maps to one Runtime
Deployment (OpenClaw, plugin, and loopback-local sidecar) and one Tool
Deployment (non-root SSH executor). OpenClaw `2026.7.1-2` uses its built-in SSH
sandbox backend. `exec`, `process`, `read`, `write`, `edit`, and `apply_patch`
are allowed on that backend; browser and control-plane tools are denied.

This topology deliberately runs the plugin with `executionBackend=hook-only`
and disables cgroup, affinity, NUMA, eBPF, and Stage-2-required collection in
the Runtime Pod. The sidecar cannot observe trustworthy Tool Pod PID/cgroup
scope, so existing absent/unattributed scope and unavailable telemetry behavior
is used. Tool lifecycle traces and advisory prediction remain active. No public
contract fields and no code under `services/scheduler/src/tool_resource` were
changed.

Local checks completed:

```bash
python deploy/two-sandbox/test_render.py
bash -n deploy/two-sandbox/cell.sh deploy/two-sandbox/smoke-test.sh scripts/two-sandbox/*.sh
cd packages/openclaw-plugin && npm.cmd test
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
