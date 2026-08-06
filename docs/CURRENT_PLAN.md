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
  `/proc/<host_pid>/cgroup`) and the eBPF exec-clause collector. Their CPU
  values cross-validate on the host.
- **read/edit** (native sandbox tools) are attributed per PID via the Docker
  observer's cgroup-diff fallback (`attribution_source=docker-exec-pid`,
  `execution.source=docker-cgroup-diff`).
- The sidecar writes JSONL traces, per-execution clause artifacts, and
  resource timelines. JSON Schema contracts in `contracts/` are the protocol
  source of truth.
- The benchmark runner (`benchmark`/`replay`) requires valid eBPF telemetry;
  a task stops before agent work if the collector cannot produce it.
- **Deep Research Bench** (`drb`) is a second benchmark journey: research
  questions run through OpenClaw with no per-task image.  The agent's tools
  execute in one very basic Docker sandbox (`python:3.11-slim` default), so
  telemetry is the read/edit/web-tool style (sandbox-container / per-PID
  docker-exec).  The relaxed gate (`runtime.gate_required`) requires an LLM
  span and a resource-sampled tool span per task, never exec-clause artifacts.
- **Legacy offline evaluation** (`legacy_eval/`) is an independent evaluator
  that runs ClawTune's prediction KBs (ClauseResourceKB / LatticeTimeKB /
  RuntimeToolResourceKB) over an external "legacy" trace dataset without
  re-running the harness.  It parses per-task Stage-2 `clause_telemetry.json`
  artifacts (validated by the native loader — they are schema-identical) plus
  `trace.jsonl`, splits tasks randomly 80/20, trains only on the 80%, replays
  the 20% predict-only, and reports per-algorithm metrics.  It never modifies
  `services/scheduler/src/tool_resource` or `tool_time`.
- **The project cold-start seed is now the legacy-trained KB.** The 80-task
  (seed-42) training KB was exported (via `legacy_eval --export-kb
  traces/tool-resource --skip-eval`) to replace the previous tiny synthetic
  seed under `traces/tool-resource/`:
  `clause-resource-kb.json` (~30 public latency nodes incl. 29 bin priors),
  `clause-lattice-time-kb.json` (3643 observations), `runtime-tool-resource-kb.json`
  (23 latency / 9 CPU nodes, plus the previous `peak_memory_mb` global prior
  preserved).  Export ships public-layer only (empty per-repo layer); memory
  prior is merged from the previous seed because legacy traces have no
  ambient-memory anchor.  The seed passes the runtime validator
  (`_validate_kb_snapshot_pair`) and is loaded as cold start by the sidecar.

## Known Limitations

- **Deep Research Bench tools are not exec clauses.** Research tools
  (web/fetch/read/edit) are measured with the sandbox-container / per-PID
  scope like read/edit in SWE-Rebench; there are no exec clause artifacts to
  cross-validate.  The DRB gate is intentionally relaxed and the report notes
  the attribution mode.  Web search defaults to **Tavily** and runs on the
  host (not the sandbox); availability depends on `TAVILY_API_KEY` reaching
  the `openclaw agent` process and the OpenClaw binary's built-in web tools.
  Each task pins `tools.web.search.provider` into its isolated OpenClaw
  config; if the host's OpenClaw lacks the provider plugin (e.g. `tavily`),
  the runner first tries to link the globally installed plugin into the
  isolated home, and only degrades to auto-detection if that is not possible
  (the warning lands in `web-search-config.log`).

- **Web-provider plugin linking targets the real plugin package.** OpenClaw
  npm plugins live at
  `<home>/.openclaw/npm/projects/<encoded-package>-<hash>`; the project dir's
  own `package.json` is the workspace manifest and lacks `openclaw.extensions`,
  so `openclaw plugins install --link <project-dir>` fails with
  `package.json missing openclaw.extensions` even when the plugin is installed.
  `_discover_web_search_provider_plugin` now prefers
  `<project>/node_modules/<package>` (whose `package.json` carries
  `openclaw.extensions`), falling back to the project dir for legacy layouts.
  A missing `TAVILY_API_KEY` still leaves `web_search` unusable even after the
  provider is pinned, so the model falls back to `exec`-based fetching.

- **DRB reuses OpenClaw's per-workspace sandbox container.** OpenClaw scopes
  Docker sandbox containers by workspace prefix and reuses a running one.
  A stale container can carry a host workspace cwd that is outside the
  container mount namespace, making every `exec`/`read`/`write` fail with
  `current working directory is outside of container mount namespace root --
  possible container breakout detected` and the run report `claw-launch was
  not executable in the sandbox`.  `deep_research_bench.host_runner` now
  preflights the launcher and removes stale sandbox containers before the
  agent runs (the same cleanup SWE-Rebench already applies), so each task
  provisions a fresh container.

- **Stale per-task trace files can false-negative a successful DRB run.**
  The task trace directory (`deep_research_bench/.runtime/traces/<task-id>/`)
  was not reset between runs, and the sidecar writes trace files there keyed
  by a run-stable runtime id.  Re-running the same task id after a broken run
  left the previous run's `shell-not-executable` spans in place, so the
  report summed them into `launcher_not_executable` and misclassified an
  otherwise successful run as FAIL (`12 managed exec call(s) failed ...`).
  `deep_research_bench.runner` now calls `_reset_task_trace_dir` before each
  task (the same reset SWE-Rebench applies), so the inspection only ever sees
  the current run's traces.

- **The DRB telemetry gate ignores in-process tool spans.** Research runs
  can legitimately call host-side in-process tools (e.g. OpenClaw's
  `session_status`) that never execute in the sandbox and therefore carry no
  sandbox resource sampling.  `_drb_required_telemetry_error` now requires
  all launcher-mode (sandbox-executed) tool spans to be sampled instead of
  demanding 100% of every tool span; it still fails on no tool spans, no
  sampled spans, or unsampled launcher spans.

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
- **Legacy continuous metrics are limited by the source format.** Legacy
  clause rows carry no `ts_start`/`ts_end` (causality unavailable; the static
  protocol observes nothing during test), and `resources.json` has no memory
  samples, so `peak_memory_mb` is not evaluated and `continuous_cpu_p90`
  coverage is low (~12% on the seed-42 run) because short clauses are CPU
  ineligible.

## Validation

### Reproducible locally (Windows/CI)

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root
cd services/scheduler && python -m pytest tests -q --basetemp ../../.pytest-tmp-scheduler
cd packages/openclaw-plugin && npm test && npm run typecheck

# Legacy offline evaluation + cold-start export (independent module; no Docker/eBPF)
python -m pytest tests/test_legacy_eval.py tests/test_legacy_eval_export.py -q --basetemp .pytest-tmp-root
python -m legacy_eval --max-train-tasks 2 --max-test-tasks 1 --print-summary
python -m legacy_eval --export-kb legacy_eval/.runtime/coldstart --skip-eval
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

Deep Research Bench acceptance (basic sandbox, no exec-clause requirement):

```bash
# Optional: Tavily web search key (wrapper allows it through sudo)
export TAVILY_API_KEY="<tavily-api-key>"
python3 scripts/clawtune.py drb --sample 1 --parallelism 1
```

Validating the stale-sandbox-container fix (host-only): a task that
previously failed with the `possible container breakout detected` error must
now complete its tools against a freshly provisioned sandbox container.  Run
the same command twice in a row for the same task id; the second run (where a
stale container from the first run exists) is the case that exercises the
pre-agent cleanup in `deep_research_bench/host_runner.py`.

Validating the stale-trace / false-negative fix (host-only): run the same DRB
task id twice.  The second run must report a clean trace dir
(`deep_research_bench/.runtime/traces/<task-id>/` contains only the current
run's `*.jsonl`), and a task whose agent completes normally must be reported
as `OK` rather than `FAIL` with `N managed exec call(s) failed ...` even if
the first (now fixed) run previously left broken spans behind.  A successful
run may still contain a small number of legitimately failed tool spans
(e.g. a first-exec `sidecar_http_503: tool_resource_stage2_start_failed:
collector attach failed` when the BCC BPF module fails to compile on the
host) — those count toward `failed_tool_span_ends` but must NOT drive the
`launcher_not_executable` FAIL classification.

To pin web search deterministically to a provider, install the provider's
plugin on the host (the runner links it into each task's isolated OpenClaw
home automatically); otherwise the runner degrades to auto-detection:

```bash
openclaw plugin install tavily && openclaw doctor --fix
```

If the plugin was installed before but is now rejected with
`package.json missing openclaw.extensions` or `plugin already exists ... delete
it first`, the `~/.openclaw` plugin registry/state is stale: force-reinstall it
(`openclaw plugins install tavily --force`), verify with `openclaw plugins
list` that `tavily` is `enabled`, then confirm the actual plugin package carries
the manifest:

```bash
python3 -c "import json; d=json.load(open('/home/<user>/.openclaw/npm/projects/openclaw-tavily-plugin-*/node_modules/@openclaw/tavily-plugin/package.json')); print(d.get('openclaw', {}).get('extensions'))"
```

A `TAVILY_API_KEY` is still required for the provider to answer; without it the
agent falls back to `exec`-based web fetching even after pinning succeeds.

Acceptance checks on the resulting `deep_research_bench/.runtime/traces/<task-id>/`:

1. `agent_prompt.txt` contains the rendered research prompt; `task_manifest.json`
   records the task id, model, sandbox image, and reference-answer bytes.
2. The trace has at least one LLM span and at least one resource-sampled
   tool span (sandbox-container / per-PID docker-exec attribution).
3. The DRB relaxed telemetry gate passes (task exits 0, `telemetry_audit.status`
   is `passed` with `mode: relaxed`).
4. The basic sandbox image (`python:3.11-slim`) is pulled; no per-task image is
   required.
5. With `TAVILY_API_KEY` set, `web_search-config.log` shows the provider pinned
   to `tavily` (`tools.web.search.provider`) and the agent emits web tool spans.

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

# NUMA-node CPU usage sampler (per-NUMA "total" utilization in each prediction)
# Expect one node entry per hardware NUMA node, e.g. 4 entries on a 4-node box.
.venv/bin/python -c "import time; from agent_scheduler.topology.linux import NumaCpuUsageSampler; s=NumaCpuUsageSampler(); time.sleep(1); print(s.sample())"

# QEMU/binfmt on Kunpeng (amd64 benchmark images)
sudo bash scripts/setup/arm_qemu_setup.sh install
sudo bash scripts/setup/arm_qemu_setup.sh check
```

This list is the authoritative record of validation commands that must run on
a Linux host.
