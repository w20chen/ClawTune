# Outstanding Validation

This file records checks that still require a suitable environment. Usage and configuration belong in the [installation guide](getting-started.md) and [benchmark guide](benchmarks.md).

- Fresh-OS package installation: `python3 scripts/clawtune.py setup` still needs acceptance on clean Ubuntu/openEuler images without preinstalled BCC/kernel tools. OpenClaw onboarding/Gateway/TUI acceptance also requires an interactive deployment.
- Real-provider acceptance across all benchmark adapters: `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 ...` requires provider credentials and the corresponding datasets; the full matrix has not been rerun with this setup revision.
- The Ubuntu GitHub Actions workflow requires a remote CI run; local Windows checks are not equivalent.
- CubeSandbox end-to-end admission and delta restore: `clawbox --output-root /data/clawbox-results experiment run local.yaml --run-id clawtune-extra-memory` requires a patched ARM64 host and registered Runtime/Tool images; local Windows validation cannot exercise VM behavior.
