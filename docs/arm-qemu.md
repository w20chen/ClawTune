# ARM/Kunpeng QEMU Docker Setup

Use this when the host is ARM/aarch64 but the SWE-Rebench task images are the
official x86_64/amd64 images.

## 1. Register amd64 binfmt

Run this once on the ARM Linux host:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh install
```

The script follows the same pattern used by `agent-test-bench`: it registers
the `amd64` handler with `tonistiigi/binfmt`, falls back to
`qemu-user-static`/`binfmt-support` on apt-based systems, and then runs an
amd64 BusyBox smoke container.

To inspect first:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh install --dry-run
bash scripts/setup/arm_qemu_setup.sh status
sudo bash scripts/setup/arm_qemu_setup.sh check
```

## 2. Tell SWE-Rebench to request amd64 images

Either set this in `swe_rebench/config.yaml`:

```yaml
docker:
  platform: "linux/amd64"
```

or override it for one run:

```bash
export SWE_REBENCH_DOCKER_PLATFORM=linux/amd64
```

This platform is passed through Docker SDK runs, Docker CLI fallback runs,
pre-pulls, host-sandbox testbed export, launcher verification, and Docker-based
cleanup of root-owned artifacts.

## 3. Run host-openclaw-sandbox

Example:

```bash
sudo -E env "PATH=$PATH" "$(command -v python3)" -m swe_rebench.runner run \
  --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export \
  --runtime-mode host-openclaw-sandbox
```

The eBPF/cgroup collectors observe the host kernel and cgroups. QEMU only
emulates the amd64 user-space inside the Docker containers, so clause timing
and cgroup attribution should still be collected from the ARM host. Hardware
counter availability may differ by Kunpeng kernel and perf policy.
