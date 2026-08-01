# Deployment

ClawTune's supported default is strict Stage-2 eBPF telemetry. The deployment
has two processes:

1. a privileged Python Scheduler sidecar on `127.0.0.1:8765`;
2. the unprivileged OpenClaw plugin, which sends model/tool lifecycle events to
   that sidecar and routes managed `exec` calls through `claw-launch`.

## Recommended Linux Deployment

Follow the complete [README eBPF-first quick start](../README.md#ebpf-first-quick-start).
The required order is:

1. install BCC, Clang/LLVM, matching kernel headers, cgroup v2, and Docker;
2. create `.venv-system` from `/usr/bin/python3` with
   `--system-site-packages`;
3. run `tools/check_stage2.py` as root and require
   `"stage2_ready": true`;
4. start the sidecar as root with that exact interpreter and kernel tree;
5. run OpenClaw normally with `managed-wrapper` and cgroup collection enabled.

Do not use a Conda interpreter with a BCC native extension built for another
Python version.

## Strict Sidecar Command

From the repository root after the preflight succeeds:

```bash
export KERNEL_BUILD="$(readlink -f "/lib/modules/$(uname -r)/build")"
STAGE2_PY="$PWD/.venv-system/bin/python"
cp .env.example .env

sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "AGENT_SCHEDULER_ENV_FILE=$PWD/.env" \
  "$STAGE2_PY" -m agent_scheduler.main \
  --host 127.0.0.1 \
  --port 8765
```

`.env.example` requires Stage-2. Managed executions fail closed if the
collector cannot start.

## Why Sidecar Auto-Start Is Off by Default

The OpenClaw plugin runs without root. A child process automatically started by
that plugin cannot safely reproduce the verified root, system-Python, BCC,
kernel-header, and cgroup environment. Automatic startup is therefore opt-in
and intended only for non-privileged diagnostic use.

Strict deployments start the sidecar explicitly and configure:

```json5
{
  "autoStartSidecar": false,
  "executionBackend": "managed-wrapper",
  "enableCgroup": true
}
```

See the [sidecar reference](sidecar.md#startup-options).

## SWE-Rebench Host Sandbox

`host-openclaw-sandbox` is the maintained complete-telemetry benchmark route.
It repeats the real BPF/cgroup/exec semantic preflight before starting a task
and fails the run if Stage-2 is unhealthy.

```bash
source .venv-system/bin/activate

sudo -E env \
  "PATH=$PATH" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "$(command -v python)" \
  -m swe_rebench.runner run \
  --config swe_rebench/config.yaml \
  --prepare \
  --dataset swe_rebench/tasks.json \
  --sample 1 \
  --export \
  --runtime-mode host-openclaw-sandbox
```

Inspect `tool_resource_preflight_host.json`, `sidecar-stderr.txt`, and the final
telemetry audit in the task trace directory.

## Docker Sidecar

```bash
docker compose up --build scheduler
```

This is useful for API development, but it is not the maintained strict eBPF
route by itself. A container needs host kernel headers, tracefs, BPF/perf
privileges, and cgroup-v2 access; simply adding `--privileged` does not provide
the correct mounts and attribution boundary automatically.

## Explicit Diagnostic Degradation

For troubleshooting an unrelated API/plugin issue only:

```bash
export AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=false
export CLAW_ENABLE_CGROUP=0
```

Such output is incomplete and must not be reported as a successful ClawTune
eBPF run.

## ARM and Kunpeng

The BPF source handles arm64 syscall wrappers, and the Scheduler/plugin are
architecture-aware. Official SWE-Rebench task images are amd64, so ARM/Kunpeng
hosts additionally require [QEMU/binfmt setup](arm-qemu.md). Real Kunpeng
kernel/BCC/PMU behavior must still pass `tools/check_stage2.py` on that host.

## Package Builds

```bash
cd services/scheduler
python -m build

cd ../../packages/openclaw-plugin
npm pack
```

## Validation

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root

cd services/scheduler
python -m pytest tests -q --basetemp ../../.pytest-tmp-scheduler

cd ../../packages/openclaw-plugin
npm test
npm run typecheck
```

The Linux-only semantic validation is:

```bash
sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "$STAGE2_PY" tools/check_stage2.py
```
