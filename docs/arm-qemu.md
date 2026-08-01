# Kunpeng and arm64 hosts

Kunpeng is a primary ClawTune target. The Scheduler, OpenClaw plugin, and eBPF
collector run natively on arm64. Official SWE-Rebench task images are commonly
amd64, so Docker needs a binfmt/QEMU handler only for those task containers.

## Normal setup

On an arm64 host, use exactly the same command as x86:

```bash
python3 scripts/clawtune.py setup
```

The command detects `aarch64`/`arm64`, installs the host's native BCC and kernel
dependencies, then runs `scripts/setup/arm_qemu_setup.sh`. That helper registers
an amd64 binfmt handler and starts a small amd64 container to prove it works.

Benchmark commands automatically set Docker's platform to `linux/amd64` on
arm64. Do not hard-code that value on x86, and do not run the eBPF sidecar in an
amd64 emulation container—the collector must match the native host kernel.

## Verify the two independent paths

Native eBPF:

```bash
python3 scripts/clawtune.py check
```

amd64 task images:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh check
```

Both checks must pass before a Kunpeng benchmark run. A successful QEMU smoke
test does not prove kernel instrumentation, and a successful eBPF test does not
prove that an amd64 image can start.

## How QEMU is installed

The helper first uses Docker's `tonistiigi/binfmt` image. This works across
openEuler and Debian-family hosts without guessing distribution package names.
If that route fails, Debian/Ubuntu hosts can fall back to
`qemu-user-static`/`binfmt-support`. On openEuler, fix access to the binfmt image
or install the site's supported qemu-user-static package before retrying.

The kernel must expose `binfmt_misc`. It may be built into the kernel; the
helper checks `/proc/filesystems` before trying `modprobe`.

## Performance expectations

Only the amd64 userspace inside the benchmark container is emulated. The
sidecar and eBPF collector remain native. CPU-heavy repository builds will be
slower than on x86, so start with `--sample 1` and raise the configured task
timeout only after observing a real timeout.

## Manual status

```bash
sudo bash scripts/setup/arm_qemu_setup.sh status
docker info --format '{{.Architecture}}'
```

If the handler disappears after reboot, rerun setup or configure the host's
systemd-binfmt service to restore it according to local operations policy.
