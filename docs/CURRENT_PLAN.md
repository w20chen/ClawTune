# Outstanding Validation

This file records checks that still require a suitable environment. Usage and configuration belong in the [installation guide](getting-started.md) and [benchmark guide](benchmarks.md).

- Fresh-machine installation: `python3 scripts/clawtune.py setup`, `python3 scripts/clawtune.py check`, and OpenClaw onboarding/Gateway/TUI acceptance require a clean Linux deployment; the existing kunpeng installation does not establish fresh-machine reproducibility.
- Benchmark guide live commands (`python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 ...`) have not all been rerun end to end through the wrapper. The kunpeng user cannot use passwordless sudo; prior live acceptance used an already privileged host context. Wrapper dry-runs do not validate elevation, credentials, image builds, or model behavior.
- Terminal tasks requiring fresh image builds: rerun the documented live command after installing compatible Compose/Buildx plugins. The existing kunpeng versions could not build through Compose; cached-image execution does not validate this path.
- `python tools/validate_pmu.py --require-reliable --concurrency 8 --max-active 8 --high-concurrency 64 --benchmark-count 40 --output .runtime/validation/pmu.json`: the full high-concurrency matrix still requires target hardware validation; small-scale checks do not establish these limits.
- The Ubuntu GitHub Actions workflow requires a remote CI run; local Windows checks are not equivalent.
