# OpenClaw Agent Scheduler

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%E2%89%A52026.7.1-6e40c9.svg)](https://openclaw.ai/)
[![Node.js](https://img.shields.io/badge/Node.js-24-green.svg?logo=node.js&logoColor=white)](https://nodejs.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

OpenClaw Agent Scheduler is an OpenClaw plugin plus a Python sidecar. Its
primary purpose is strict Stage-2 eBPF collection of clause-level CPU and RSS
telemetry, together with OpenClaw model/tool traces and SWE-Rebench execution.

## Supported Default

The default workflow is Linux-only and fails closed when eBPF telemetry is not
healthy. It requires:

- root for the maintained Stage-2 collector;
- BCC/BPF Python bindings (`bcc` or openEuler's `bpfcc`);
- Clang/LLVM and development headers matching the running kernel;
- cgroup v2 and writable tracefs/kprobe controls;
- Docker for OpenClaw sandbox and SWE-Rebench integration;
- Python 3.10+, Node.js/npm, and OpenClaw 2026.7.1+.

Process/cgroup-only collection exists solely as an explicit troubleshooting
mode. Its output is not complete ClawTune Stage-2 telemetry.

## eBPF-First Quick Start

Run every command from the repository root. Use the system Python family for
Stage-2; do not mix a Conda Python with BCC compiled for `/usr/bin/python3`.

### 1. Install host packages

Install BCC, its Python binding, Clang/LLVM, `bpftool`, Docker, and the kernel
development package for the exact output of `uname -r`.

On Debian/Ubuntu the package set is commonly:

```bash
sudo apt-get install -y \
  bpfcc-tools python3-bpfcc clang llvm bpftool \
  "linux-headers-$(uname -r)"
```

openEuler/EulerOS package names vary by release. Install the distribution BCC
and kernel-devel packages, then verify that system Python imports `bpfcc`:

```bash
/usr/bin/python3 -c 'import bpfcc; print(bpfcc.__file__)'
```

ClawTune supports both `bcc` and `bpfcc`; no compatibility symlink is needed.

### 2. Create one Stage-2 Python environment

`--system-site-packages` makes the distribution BCC binding visible while pip
installs the Scheduler dependencies into an isolated environment:

```bash
/usr/bin/python3 -m venv --system-site-packages .venv-system
source .venv-system/bin/activate
python -m pip install --upgrade pip
python -m pip install -e "services/scheduler[dev]"

cd packages/openclaw-plugin
npm install
npm run build
cd ../..
```

Check that NumPy and a BCC binding belong to this selected interpreter:

```bash
python - <<'PY'
import importlib
import numpy
import sys

print("Python:", sys.executable, sys.version)
print("NumPy:", numpy.__version__)
for name in ("bcc", "bpfcc"):
    try:
        module = importlib.import_module(name)
    except ImportError:
        continue
    print("BCC:", module.__name__, module.__file__)
    break
else:
    raise SystemExit("neither bcc nor bpfcc is importable")
PY
```

### 3. Run the strict Stage-2 preflight

This single command compiles the complete embedded BPF program, attaches the
real probes and perf sampler, creates a test cgroup, executes a command, and
rejects empty/lost lifecycle data:

```bash
export KERNEL_BUILD="$(readlink -f "/lib/modules/$(uname -r)/build")"
STAGE2_PY="$PWD/.venv-system/bin/python"

sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "$STAGE2_PY" tools/check_stage2.py \
  --output /tmp/clawtune-stage2-preflight.json
```

Continue only when the command exits `0` and reports:

```json
{"stage2_ready": true}
```

This preflight covers the openEuler `bpfcc` binding and both pre-6.2 and
Linux 6.2+ `mm_struct::rss_stat` layouts. See
[troubleshooting](docs/troubleshooting.md) for any other result.

### 4. Start the strict sidecar

```bash
cp .env.example .env

sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "AGENT_SCHEDULER_ENV_FILE=$PWD/.env" \
  "$STAGE2_PY" -m agent_scheduler.main \
  --host 127.0.0.1 \
  --port 8765
```

Leave this terminal running. In another terminal:

```bash
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
```

The example `.env` keeps
`AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=true`, so managed executions
fail instead of silently accepting coarse telemetry.

### 5. Install and configure the OpenClaw plugin

The plugin does not auto-start the sidecar by default: an unprivileged Node
child cannot reproduce the verified root/Python/BCC environment.

```bash
source .venv-system/bin/activate
openclaw plugins install --link ./packages/openclaw-plugin
openclaw plugins enable agent-scheduler

LAUNCHER_PATH="$(command -v claw-launch)"
cat <<JSON5 | openclaw config patch --stdin
{
  plugins: {
    entries: {
      "agent-scheduler": {
        enabled: true,
        config: {
          endpoint: "http://127.0.0.1:8765",
          autoStartSidecar: false,
          recordRawTrace: true,
          executionBackend: "managed-wrapper",
          launcherPath: "$LAUNCHER_PATH",
          enableCgroup: true,
          securityBoundaryAccepted: true
        }
      }
    }
  }
}
JSON5
```

### 6. Configure the model proxy and verify a real tool call

Set the OpenClaw provider base URL to `http://127.0.0.1:8765/v1`. Provider
examples are in the [operator guide](docs/operator-guide.md).

```bash
openclaw agent --local --agent main --model "vllm/<your-model>" \
  --message "Use the shell to run: python -c 'print(\"trace-ok\")'. Then summarize the result." \
  --session-key "clawtune-ebpf-smoke"

curl -fsS "http://127.0.0.1:8765/v1/tools/recent?limit=5"
```

A complete result must include a managed execution, cgroup/process
attribution, a finalized Stage-2 artifact, and a healthy collector without
telemetry loss. HTTP health alone proves only that the API process is alive;
the Stage-2 preflight and real tool call prove the kernel path.

## Run SWE-Rebench with Required eBPF

```bash
source .venv-system/bin/activate
cp swe_rebench/config.example.yaml swe_rebench/config.yaml
export LLM_API_KEY="<your-provider-key>"

python -m swe_rebench.discover --sample 20 --out swe_rebench/tasks.json

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

CLI-selected `host-openclaw-sandbox` mode makes Stage-2 required by default
and runs another semantic preflight before releasing the task.

Official SWE-Rebench task images are amd64. ARM/Kunpeng hosts must first
complete the [QEMU/binfmt setup](docs/arm-qemu.md) and select `linux/amd64`.

## Troubleshooting-Only Degraded Run

When diagnosing an unrelated model/plugin issue, you may temporarily set:

```bash
export AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=false
export CLAW_ENABLE_CGROUP=0
```

This is fail-open diagnostic operation, not a successful eBPF deployment and
not acceptable for complete telemetry evaluation. Restore strict mode before
collecting results.

## Documentation

- [Troubleshooting, Python/BCC, cgroup, and kernel errors](docs/troubleshooting.md)
- [OpenClaw provider configuration and trace smoke test](docs/operator-guide.md)
- [Sidecar reference](docs/sidecar.md)
- [Deployment modes](docs/deployment.md)
- [SWE-Rebench guide](swe_rebench/README.md)
- [ARM/Kunpeng and QEMU](docs/arm-qemu.md)
- [Architecture](docs/architecture.md)
- [Trace schema v6](docs/trace-schema-v6.md)
