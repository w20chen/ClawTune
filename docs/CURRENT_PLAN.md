# Outstanding Validation

This file records checks that still require a suitable environment. Usage and configuration belong in the [installation guide](getting-started.md) and [benchmark guide](benchmarks.md).

- Fresh-OS package installation: `python3 scripts/clawtune.py setup` still needs acceptance on clean Ubuntu/openEuler images without preinstalled BCC/kernel tools. OpenClaw onboarding/Gateway/TUI acceptance also requires an interactive deployment.
- Real-provider acceptance across all benchmark adapters: `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 ...` requires provider credentials and the corresponding datasets; the full matrix has not been rerun with this setup revision.
- The Ubuntu GitHub Actions workflow requires a remote CI run; local Windows checks are not equivalent.
