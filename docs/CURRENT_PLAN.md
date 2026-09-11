# Outstanding Validation

This file records checks that cannot run in the current environment. Operational and development commands belong in the [installation guide](getting-started.md); task execution commands belong in the [benchmark guide](benchmarks.md).

## Windows workspace limitations

- `python3 scripts/clawtune.py setup` and `python3 scripts/clawtune.py check`: require Linux BCC/eBPF, cgroup v2, matching kernel headers, and privileges; not run here.
- OpenClaw onboarding, Gateway/TUI, and live agent commands in the installation guide: require the Linux deployment and provider configuration; fresh-machine end-to-end acceptance remains outstanding.
- Live commands for all five benchmark adapters: require Docker, task inputs/images, credentials, and applicable search/function dependencies. Dry-runs do not replace execution.
- `sudo bash scripts/setup/arm_qemu_setup.sh check`: requires an arm64 Linux host to verify native collection and amd64 container execution.
- `python tools/validate_pmu.py --require-reliable --concurrency 8 --max-active 8 --high-concurrency 64 --benchmark-count 40 --output .runtime/validation/pmu.json`: requires target Linux hardware counters; accuracy and descendant inheritance are unverified here.
- Dataset download commands, native BFCL category loading, and Terminal Compose execution: external data and dependencies are not provisioned locally.
- The Ubuntu GitHub Actions workflow requires a remote CI run; local Windows checks are not equivalent.

Linux acceptance should also cover parallel task isolation, timeout/Ctrl+C cleanup, completed persistence, and resume eligibility.
