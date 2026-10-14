# Outstanding validation

This file contains unresolved checks and their prerequisites. Usage and configuration belong in [getting-started.md](getting-started.md) and [benchmarks.md](benchmarks.md).

The active implementation specification is [COMMAND_PARSER_PLAN.md](COMMAND_PARSER_PLAN.md).

## Local validation prerequisites

- Native Windows cannot complete `python -m pytest -p no:cacheprovider services/sidecar/tests/test_kb_shared_config.py services/sidecar/tests/test_edge_kappa_kb.py services/sidecar/tests/test_lattice_time_kb.py services/sidecar/tests/test_kb_algorithm_equivalence.py services/sidecar/tests/test_lattice_resources.py -q`: parser-dependent cases require POSIX pipe polling (`WinError 10038`). Run this selection in the Linux/WSL environment below.

- Run parser/POSIX tests in Linux or WSL with the sidecar on `PYTHONPATH`, an activated Python environment, pytest, setuptools, and a working mvdan parser. Native Windows parser tests fail with `WinError 10038`; Windows-mounted temporary directories do not preserve POSIX permission bits. Use:

  ```sh
  PYTHONPATH=services/sidecar/src:. python -m pytest services/sidecar/tests tests -q -rs --basetemp /tmp/clawtune-tests
  ```

- On PowerShell, use `npm.cmd test` in `packages/clawtune-plugin`; `npm test` may be blocked by execution policy. Invoke WSL validation through a Bash script when inline Python quoting is not preserved.
- `npm.cmd test --prefix packages/clawtune-plugin` on Windows skips three POSIX graceful-shutdown cases. Complete `npm test --prefix packages/clawtune-plugin` with native Linux Node.js/npm; the current WSL environment exposes Windows npm but has no native `node` executable.
- WSL's system `python3` lacks pytest: `wsl.exe --exec env PYTHONPATH=services/sidecar/src:. python3 -m pytest services/sidecar/tests tests -q -rs --basetemp /tmp/clawtune-review-20261012` cannot run with that interpreter. Use an activated Python environment with the sidecar test dependencies.
- Repository-wide Ruff cleanup remains separate work: `python -m ruff check services/sidecar/src services/sidecar/tests tests cold_start offline scripts/build_bootstrap_seed.py swe_rebench/host_openclaw.py`. Existing findings must be separated from parser-change findings.
- Native Windows Python lacks Ruff (`No module named ruff`). Run `python -m ruff check services/sidecar/src/tool_time/incremental.py services/sidecar/src/edge_kappa_kb/snapshot.py services/sidecar/src/kb_common/feature_index.py services/sidecar/tests/test_kb_incremental_equivalence.py research/kb_incremental_20261013` with the WSL validation environment.

## Target Linux runtime and collectors

- The full Python regression run skips the opt-in native cgroup test. On a Linux host with delegated cgroup v2, run `CLAWTUNE_TEST_NATIVE_CGROUP=1 PYTHONPATH=services/sidecar/src:. python -m pytest tests/test_benchmark_scope_unification.py::test_live_native_worker_cgroup_and_memory -q`.

- ClawBox's LatticeKB integration still needs a rebuilt Runtime/Tool image pair
  and a real CubeSandbox run using the same ClawTune source export. Run the image
  build commands in ClawBox's installation guide, then exercise predicted
  admission with a seed containing eligible guest clause-memory measurements.
  Run `CGO_ENABLED=1 go test -race ./...` from ClawBox's `toolbridge` directory
  on Linux with a C compiler.

Deployment and live acceptance require Linux, Docker, OpenClaw, a funded provider, BCC/kernel headers, BPF/perf privileges, and appropriate cgroup v2 delegation.

On the target host, run:

```sh
python3 tools/check_ebpf.py
python3 tools/validate_pmu.py --concurrency 4 --require-reliable
PYTHONPATH=services/sidecar/src:. python3 -m pytest services/sidecar/tests tests -q -rs --basetemp /tmp/clawtune-tests
```

- Verify eBPF attach, event delivery/loss, drain timing, CPU boundary deltas, actual sampling gaps, four-event PMU running ratios, and per-execution ownership against independent counters. Include sleeping memory holders, short and long calls, concurrent calls, and shared-runtime non-exec tools.
- Validate both normal collection and injected collector failure. Set `CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false` for dedicated-cgroup fallback acceptance and `true` for strict eBPF acceptance. Missing/shared scopes must remain unavailable; never substitute container totals or cgroup charge peaks for action RSS.
- Verify stable environment-memory scope across migration and cleanup, serial payloads longer than the sampler interval, missing baselines, overlapping executions, and native read/edit windows. Check actual monotonic clock compatibility. Missing/foreign clocks or sparse measurements must not produce eligible training labels.
- RSS coverage needs an independent reference for address spaces with a single sample or missing lifetime edges; dense samples from peers must not mask incomplete coverage.

## Live benchmark acceptance

- Cold-start transfer study: fresh measurements could not run locally because neither Windows nor WSL exposes Docker (`command -v docker` returned no path). On a prepared Linux collector host, run `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --dataset <multi-repository-task-roster.json> --sample 30 --parallelism 1 --output <new-study-run>`; retain multiple independent tasks per repository, native/QEMU identity, quota, and eligible environment extra-memory labels. Do not substitute a dry-run for this validation. Existing-trace findings are in `research/cold_start_transfer`.

Use plugin and sidecar from the same checkout and a fresh run-local KB. These commands remain unvalidated on the final target deployment:

```sh
python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 4 --parallelism 2
python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3
python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 --task-timeout-seconds 60
```

Apply the matrix to `swe-rebench`, `swe-bench-verified`, `deep-research-bench`, `terminal-bench`, and `bfcl`, with sufficient configured tasks and provider access. Verify roster size before requiring multiple tasks.

- Check independent Tool/Trie/Lattice/Edge predictions, valid unavailable outputs, completion admission, concurrent delivery, restart/drain durability, checkpoints, and final KB flush. Measure per-model/per-target coverage and paired errors; unsupported resources and consumer-scope changes are not prediction improvements. Confirm censor bounds from source telemetry before training censored outcomes.
- Validate retained workload duration, CPU interval deltas, and environment-memory labels against real execution windows. Historical replay and unit tests cannot establish new-run measurement accuracy or missing resource labels.
- Exercise timeout and interruption during setup and active tools; confirm no surviving producers, preserved first timeout records, and acknowledged KB flush. A Terminal native timeout below the harness budget must not truncate the simulation independently.
- Confirm documented output paths and artifact semantics for every adapter, including per-turn logs and Terminal environment/log directories.
- For cross-process ownership and PMU validation, run `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 6 --parallelism 2` and the equivalent `--benchmark terminal-bench` concurrently, using datasets with at least six tasks each.
- Establish Edge-specific benefit over fixed/shared-child weights on task-paired data separately from integration acceptance.

## Remaining deployment checks

- `python3 scripts/clawtune.py setup` requires clean Ubuntu/openEuler images without preinstalled BCC/kernel tools. Interactive OpenClaw onboarding and TUI still need deployment checks; Ubuntu CI requires an actual remote workflow run.
- The pristine task-image dependency check requires target-host access. Run `docker run --rm --pull never --network none --read-only --platform linux/amd64 --entrypoint /opt/miniconda3/envs/testbed/bin/python swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64 -B -c 'import sys,pydantic; print(sys.executable); print(sys.version); print(pydantic.__version__)'` on the target host.
- CubeSandbox admission and delta restore require a patched ARM64 host and registered Runtime/Tool images: `clawbox --output-root /data/clawbox-results experiment run local.yaml --run-id clawtune-extra-memory`.

## Command-rule target acceptance

- Extend isolated-help target acceptance to output overflow, child-process cleanup, timeout and concurrent collector windows. These cases are not established by the basic Docker help acceptance test. Use a Linux Docker host with an already pulled image containing `python3` and extend `CLAWTUNE_TEST_HELP_IMAGE=<image> PYTHONPATH=services/sidecar/src:. python -m pytest services/sidecar/tests/test_command_help_isolation.py -q -rs`.
- Registered-workspace script discovery and edit invalidation still require live acceptance: `PYTHONPATH=services/sidecar/src:. python scripts/command_rules/check_live_generation.py <workspace> <relative-script>` needs a disposable workspace containing an argument-parser script and configured upstream URL/key/model. Verify initial partial parse, cache activation, parser/helper edits and container-source invalidation in the deployed service; isolated unknown-tool generation does not establish these paths.

## KB online latency acceptance

- Default-order-6 shared-writer latency remains unmeasured. Resume the component probe with `PYTHONHASHSEED=42 PYTHONPATH=services/sidecar/src:. python research/kb_incremental_20261013/run.py --root <immutable-study-root> --out <result-dir-with-edge-6-trained.json> --cases pair:6:900 --rss-gib 40`. Its time and RSS limits apply only to this experiment.
- At default order 6, measure the complete HTTP path under sustained concurrent arrivals at increasing history sizes: query latency, completion-to-prediction visibility, and durable checkpoint throughput separately. Include raw parsing, compatibility diagnostics, telemetry delivery, snapshot capture/serialization, and StateStore checkpointing. Preserve actual labels. Incremental local statistics and component probes do not establish an HTTP latency or freshness SLA; the missing measurements do not by themselves justify reducing order 6.
