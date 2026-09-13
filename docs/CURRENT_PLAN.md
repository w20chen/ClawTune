# Outstanding Validation

This file records checks that still require a suitable environment. Usage and configuration belong in the [installation guide](getting-started.md) and [benchmark guide](benchmarks.md).

- Fresh-OS package installation: `python3 scripts/clawtune.py setup` still needs acceptance on clean Ubuntu/openEuler images without preinstalled BCC/kernel tools. OpenClaw onboarding/Gateway/TUI acceptance also requires an interactive deployment.
- Real-provider acceptance across all benchmark adapters: `python3 scripts/clawtune.py benchmark --benchmark <name> --sample 3 --parallelism 3 ...` requires provider credentials and the corresponding datasets; the full matrix has not been rerun with this setup revision.
- `python tools/validate_pmu.py --require-reliable --concurrency 8 --max-active 8 --high-concurrency 64 --benchmark-count 40 --output .runtime/validation/pmu.json`: the full high-concurrency matrix still requires target hardware validation; small-scale checks do not establish these limits.
- The Ubuntu GitHub Actions workflow requires a remote CI run; local Windows checks are not equivalent.
- `ssh kunpeng 'git -C <checkout> rev-parse HEAD'`: remote checkout verification remains pending. The linked upstream snapshots do not establish existing-run provenance.
