# Outstanding Validation

This file records checks that cannot run in the current environment. Operational and development commands belong in the [installation guide](getting-started.md); task execution commands belong in the [benchmark guide](benchmarks.md).

## Windows workspace limitations

- PowerShell `npm run typecheck --prefix packages/clawtune-plugin` is blocked by the local script execution policy; use `npm.cmd` to run the same validation.

- `python3 scripts/clawtune.py setup` and `python3 scripts/clawtune.py check`: require Linux BCC/eBPF, cgroup v2, matching kernel headers, and privileges; not run here.
- OpenClaw onboarding, Gateway/TUI, and live agent commands in the installation guide: require the Linux deployment and provider configuration; fresh-machine end-to-end acceptance remains outstanding.
- Live commands for all five benchmark adapters: require Docker, task inputs/images, credentials, and applicable search/function dependencies. Dry-runs do not replace execution.
- `sudo bash scripts/setup/arm_qemu_setup.sh check`: requires an arm64 Linux host to verify native collection and amd64 container execution.
- `python tools/validate_pmu.py --require-reliable --concurrency 8 --max-active 8 --high-concurrency 64 --benchmark-count 40 --output .runtime/validation/pmu.json`: requires target Linux hardware counters; accuracy and descendant inheritance are unverified here.
- Dataset download commands, native BFCL category loading, and Terminal Compose execution: external data and dependencies are not provisioned locally.
- The Ubuntu GitHub Actions workflow requires a remote CI run; local Windows checks are not equivalent.

Linux acceptance should also cover parallel task isolation, timeout/Ctrl+C cleanup, completed persistence, and resume eligibility.

## Optional image-cache preparation on kunpeng

- `sudo -n docker compose version` could not run because root has no Compose plugin. The adapter now falls back to the invoking user's installed Compose executable; real root-context Compose up/exec/down passed on kunpeng without changing root's configuration.
- The optional cache helper was checked against the original two kunpeng preparation manifests: selected task IDs, 68 image references, 30 Dockerfile contents and build options matched. Unit/adapter tests, registry probes, a real Docker pull, and a detached amd64 build smoke check passed on kunpeng. Usage is included in [benchmark task preparation](benchmarks.md#2-prepare-tasks).
- Full five-benchmark end-to-end commands were not run by this cache validation: they require the runtime prerequisites above, credentials, and potentially lengthy upstream builds. Background cache-job completion is tracked separately and must not be presented as benchmark correctness or scoring validation.

## Runtime-fix regression validation

- On kunpeng, `.venv/bin/python -m pytest tests/test_benchmark_runtime_fixes.py tests/test_swe_rebench_selection.py tests/test_demo_workflows.py services/sidecar/tests/test_tool_resource_telemetry.py -q -p no:cacheprovider --basetemp /home/weitianc/clawtune-image-prepull/runtime-fixes/pytest-final-root` passed all 212 tests in the privileged host context used by benchmarks. The corresponding Windows run passed 210 tests with two POSIX-only skips.
- The initial unprivileged run could not use the configured `/home/.pytest-tmp`; an explicit writable `--basetemp` resolves this. The existing read-only trace-cleanup test still fails as an unprivileged user; it passes in the privileged benchmark context. This change does not fix that separate unprivileged cleanup limitation.
- Live kunpeng checks verified 25/64 arguments captured fully, 65 arguments marked capped, a 25-argument expanded glob aligned successfully, incomplete evidence still rejected, and cached amd64 image export without a pull. These checks do not replace a full model-backed benchmark run.
- Terminal 30-task launch preflight: Compose 5.5.1 `up -d --build` cannot use the installed Buildx 0.15.1 (requires >=0.17). `DOCKER_BUILDKIT=0` bypasses that check but the legacy Compose build ignored the requested architecture and exceeded a 120-second smoke budget while reinstalling dependencies. Build-cache reuse through this path remains unverified. The detached test batch instead uses separate task copies pinned to the 30 previously built image IDs; original task files remain untouched. A cached task's real up/exec/down passed. Nested sudo in the privileged launch reset the invoking user, so the launcher invokes `python -m benchmarks.cli` directly in that already privileged context.
