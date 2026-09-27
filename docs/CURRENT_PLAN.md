# Outstanding validation

This file contains unresolved checks and their prerequisites. Usage and configuration belong in [getting-started.md](getting-started.md) and [benchmarks.md](benchmarks.md).

## Local validation prerequisites

- Run parser/POSIX tests in Linux or WSL with the sidecar on `PYTHONPATH`, an activated Python environment, pytest, setuptools, and a working mvdan parser. Native Windows parser tests fail with `WinError 10038`; Windows-mounted temporary directories do not preserve POSIX permission bits. Use:

  ```sh
  PYTHONPATH=services/sidecar/src:. python -m pytest services/sidecar/tests tests -q -rs --basetemp /tmp/clawtune-tests
  ```

- On PowerShell, use `npm.cmd test` in `packages/clawtune-plugin`; `npm test` may be blocked by execution policy. Invoke WSL validation through a Bash script when inline Python quoting is not preserved.
- Ruff validation remains outstanding. Earlier environments either lacked Ruff or reported pre-existing findings; do not treat those attempts as a clean lint result. Run `python -m ruff check services/sidecar/src services/sidecar/tests tests cold_start offline scripts/build_bootstrap_seed.py swe_rebench/host_openclaw.py` in an environment with Ruff installed, and distinguish existing findings from changed-code findings.

## Target Linux runtime and collectors

- ClawBox's LatticeKB integration still needs a rebuilt Runtime/Tool image pair
  and a real CubeSandbox run using the same ClawTune source export. Run the image
  build commands in ClawBox's installation guide, then exercise predicted
  admission with a seed containing eligible guest clause-memory measurements.
  Run `CGO_ENABLED=1 go test -race ./...` from ClawBox's `toolbridge` directory
  on Linux with a C compiler; the available WSL environment lacks GCC.

Deployment and live acceptance require Linux, Docker, OpenClaw, a funded provider, BCC/kernel headers, BPF/perf privileges, and appropriate cgroup v2 delegation. The last `ssh -o BatchMode=yes -o ConnectTimeout=10 kunpeng "pwd"` attempt timed out; previous live runs also encountered provider HTTP 402. These prerequisites must be restored before live acceptance.

On the target host, run:

```sh
python3 tools/check_ebpf.py
python3 tools/validate_pmu.py --concurrency 4 --require-reliable
PYTHONPATH=services/sidecar/src:. python3 -m pytest services/sidecar/tests tests -q -rs --basetemp /tmp/clawtune-tests
CLAWTUNE_TEST_NATIVE_CGROUP=1 PYTHONPATH=services/sidecar/src:. python3 -m pytest tests/test_benchmark_scope_unification.py -q
```

- The native-cgroup test requires delegated Linux cgroup v2 and remains opt-in. The target Conda interpreter previously lacked `os.pidfd_open`, causing descendant-cleanup failures; use a suitable interpreter before accepting producer cleanup. WSL tests do not validate the target kernel or remote CI.
- Verify eBPF attach, event delivery/loss, drain timing, CPU boundary deltas, actual sampling gaps, four-event PMU running ratios, and per-execution ownership against independent counters. Include sleeping memory holders, short and long calls, concurrent calls, and shared-runtime non-exec tools.
- Validate both normal collection and injected collector failure. Set `CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false` for dedicated-cgroup fallback acceptance and `true` for strict eBPF acceptance. Missing/shared scopes must remain unavailable; never substitute container totals or cgroup charge peaks for action RSS.
- Verify stable environment-memory scope across migration and cleanup, serial payloads longer than the sampler interval, missing baselines, overlapping executions, and native read/edit windows. Check actual monotonic clock compatibility. Missing/foreign clocks or sparse measurements must not produce eligible training labels.
- RSS coverage needs an independent reference for address spaces with a single sample or missing lifetime edges; dense samples from peers must not mask incomplete coverage.

## Live benchmark acceptance

Use plugin and sidecar from the same checkout and a fresh run-local KB. These commands remain unvalidated on the final target deployment:

```sh
python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 4 --parallelism 2
python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3
python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 --task-timeout-seconds 60
```

Apply the matrix to `swe-rebench`, `swe-bench-verified`, `deep-research-bench`, `terminal-bench`, and `bfcl`, with sufficient configured tasks and provider access. Earlier smoke rosters for some adapters contained only one task; verify roster size before requiring multiple tasks.

- Check independent Tool/Trie/Lattice/Edge predictions, valid unavailable outputs, completion admission, concurrent delivery, restart/drain durability, checkpoints, and final KB flush. Measure per-model/per-target coverage and paired errors; unsupported resources and consumer-scope changes are not prediction improvements. Confirm censor bounds from source telemetry before training censored outcomes.
- Validate retained workload duration, CPU interval deltas, and environment-memory labels against real execution windows. Historical replay and unit tests cannot establish new-run measurement accuracy or missing resource labels.
- Exercise timeout and interruption during setup and active tools; confirm no surviving producers, preserved first timeout records, and acknowledged KB flush. A Terminal native timeout below the harness budget must not truncate the simulation independently.
- Confirm documented output paths and artifact semantics for every adapter, including per-turn logs and Terminal environment/log directories.
- For cross-process ownership and PMU validation, run `python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 6 --parallelism 2` and the equivalent `--benchmark terminal-bench` concurrently, using datasets with at least six tasks each.
- Establish Edge-specific benefit over fixed/shared-child weights on task-paired data separately from integration acceptance.

## Remaining deployment checks

- `python3 scripts/clawtune.py setup` requires clean Ubuntu/openEuler images without preinstalled BCC/kernel tools. OpenClaw onboarding/Gateway/TUI requires interactive deployment; Ubuntu CI requires an actual remote workflow run.
- The pristine task-image dependency check remains blocked by remote access. Run `docker run --rm --pull never --network none --read-only --platform linux/amd64 --entrypoint /opt/miniconda3/envs/testbed/bin/python swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64 -B -c 'import sys,pydantic; print(sys.executable); print(sys.version); print(pydantic.__version__)'` on the target host.
- CubeSandbox admission and delta restore require a patched ARM64 host and registered Runtime/Tool images: `clawbox --output-root /data/clawbox-results experiment run local.yaml --run-id clawtune-extra-memory`.
