# Current Plan

This document states the current supported behavior, the remaining known
limitations, and the validation commands that cannot run in a Windows
development workspace (they require a Linux host with Docker, cgroup v2,
BCC/eBPF privileges, and a configured launcher/sidecar). Per-change history
lives in git history; this file intentionally does not duplicate it.

## Current State

ClawTune provides one eBPF-required user journey from host setup through
OpenClaw use and SWE-Rebench execution, on Kunpeng/arm64 (primary) and x86_64
Linux.

- **exec** is measured by two independent monitors: a cgroup-v2 scope
  (`cpu.stat`/`memory.current`/`io.stat`, derived from
  `/proc/<host_pid>/cgroup`) and the eBPF Stage-2 clause collector. Their CPU
  values cross-validate on the host.
- **read/edit** (native sandbox tools) are attributed per PID via the Docker
  observer's cgroup-diff fallback (`attribution_source=docker-exec-pid`,
  `execution.source=docker-cgroup-diff`).
- The sidecar writes JSONL traces, per-execution clause artifacts, and
  resource timelines. JSON Schema contracts in `contracts/` are the protocol
  source of truth.
- The benchmark runner (`benchmark`/`replay`) requires valid eBPF telemetry;
  a task stops before agent work if the collector cannot produce it.

## Known Limitations

- **read/edit CPU is container-cgroup level, not per-PID.** Attribution is
  per-PID, but the CPU figure comes from the shared sandbox container cgroup
  because the short-lived tool process exits before the completion snapshot.
  In serial execution this numerically equals the tool's own CPU.
- **exec network is namespace-wide.** For the derived container-cgroup scope,
  the top-level net aggregate uses the namespace `/proc/net/dev` window; it is
  not per-PID BCC (a shared container cgroup cannot safely reset the per-tgid
  BCC tracker while tools overlap).
- **`host_cgroup_gate` stays false in benchmarks.** The benchmark sandbox runs
  the launcher in `fork-exec` mode, which reports `host_cgroup_gate=false` by
  design. The sidecar derives the host cgroup from `/proc/<host_pid>/cgroup`
  instead, so launcher spans remain cgroup-backed.

## Validation

### Reproducible locally (Windows/CI)

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root
cd services/scheduler && python -m pytest tests -q --basetemp ../../.pytest-tmp-scheduler
cd packages/openclaw-plugin && npm test && npm run typecheck
```

### Host-only (cannot run in this Windows workspace)

These require a Linux host with Docker, cgroup v2, BCC/eBPF privileges,
matching kernel headers, and a configured `claw-launch`/sidecar:

```bash
# Full eBPF collector + benchmark acceptance (Kunpeng: amd64 images via QEMU)
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py check
python3 scripts/clawtune.py benchmark --sample 1 --parallelism 1
```

Acceptance checks on the resulting `swe_rebench/.runtime/traces/<task-id>/`:

1. `tool_resource_preflight_host.json` passes; no exec result is
   `shell-not-executable`.
2. Every executed launcher call has one healthy clause-telemetry artifact
   (`artifact_summary.collector.health == "healthy"`,
   `telemetry_quality == "ok"`).
3. Launcher spans are cgroup-backed: `resources.scope == "cgroup"`,
   `monitor_source == "cgroup-v2"`, `cgroup_cpu_time_s` populated, and no
   `cpu_time_s == 0`.
4. read/edit spans are per-PID: `attribution_source == "docker-exec-pid"`,
   `execution.source == "docker-cgroup-diff"`, distinct `payload_pid`.
5. `docker_exec_observer_diagnostics.json` shows `cgroup_diff_captures > 0`.
6. `resources.coverage_ratio` (when non-null) is in `[0, 1]`.
7. The benchmark's required-telemetry gate passes (task exits 0).

Optional host probes:

```bash
# per-PID BCC net accounting availability (set BCC_KERNEL_SOURCE to the
# running kernel's build tree)
cd /home/<user>/ClawTune && \
  BCC_KERNEL_SOURCE=/usr/src/kernels/$(uname -r) \
  .venv/bin/python -c "import os; from agent_scheduler.monitoring.net_accounting import ProcessNetAccounting as A; a=A([os.stat('/proc/self/ns/net').st_ino]); print('net available =', a.available); print(a._attach_error)"

# QEMU/binfmt on Kunpeng (amd64 benchmark images)
sudo bash scripts/setup/arm_qemu_setup.sh install
sudo bash scripts/setup/arm_qemu_setup.sh check
```

This list is the authoritative record of validation commands that must run on
a Linux host.
