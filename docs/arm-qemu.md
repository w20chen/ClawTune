# ARM/Kunpeng QEMU Docker Setup

Use this when the host is ARM/aarch64 (e.g. Kunpeng 920) but the SWE-Rebench
task images are the official x86_64/amd64 images.

## Prerequisites

The base environment is the same as x86: Python 3.10+, Node.js, OpenClaw CLI
2026.7.1+, Docker. Follow [deployment.md](deployment.md) first, then return
here for ARM-specific QEMU setup.

## Why QEMU?

SWE-Rebench publishes only x86_64 task images. On an aarch64 host, Docker
cannot run these natively — the CPU instruction sets differ. QEMU emulates an
amd64 CPU in userspace so the container's `/bin/bash`, `python`, `gcc`, etc.
can execute. The kernel (cgroups, eBPF, perf) still runs natively on aarch64,
so resource telemetry is collected from the host side without emulation
overhead.

## 1. Register amd64 binfmt

`binfmt_misc` is a Linux kernel mechanism that tells the kernel: "when you see
an x86_64 ELF binary, don't try to execute it directly — hand it to QEMU
instead." This registration is per-host, survives reboots only if the binfmt
service is enabled.

Run this once on the ARM Linux host:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh install
```

The script registers the `amd64` handler with `tonistiigi/binfmt`, falls back
to `qemu-user-static`/`binfmt-support` on apt-based systems, and runs an
amd64 BusyBox smoke test after setup.

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

## Next: Run SWE-Rebench

After completing QEMU setup, proceed to [../swe_rebench/README.md](../swe_rebench/README.md)
for task discovery and batch running. Use `host-openclaw-sandbox` mode.

## Kunpeng Troubleshooting

**`docker pull` gets wrong architecture (aarch64 instead of amd64):**
`docker: no matching manifest for linux/arm64` means Docker tried the host
arch. Make sure `docker.platform: "linux/amd64"` is set in `swe_rebench/config.yaml`
before `prepare`, or export `SWE_REBENCH_DOCKER_PLATFORM=linux/amd64`.

**binfmt registration lost after reboot:**
The `tonistiigi/binfmt` container only registers handlers for the current boot.
Re-run `sudo bash scripts/setup/arm_qemu_setup.sh install` after reboot, or
configure the systemd `binfmt-support` service for persistence.

**QEMU emulation too slow / task timeout:**
QEMU userspace emulation adds 2-10x CPU overhead. Increase
`batch.task_timeout_seconds` in `swe_rebench/config.yaml` compared to x86
runs. Heavy compilation tasks (e.g. C/C++ projects in SWE-bench) are the most
affected.

**Perf hardware counters missing on Kunpeng:**
Kunpeng 920 uses ARM PMUv3. Some `perf` hardware events differ from x86.
If `tool_resource_preflight_host.json` reports missing counters, Stage-2 eBPF
CPU-clock sampling still works (it uses `PERF_COUNT_SW_CPU_CLOCK`, which is
software-based). Use `--no-stage2-required` only if you intentionally accept
incomplete telemetry.
