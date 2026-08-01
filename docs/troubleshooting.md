# Troubleshooting

Use this guide after trying the README eBPF-first quick start. Strict Stage-2
eBPF is the supported default; a degraded process/cgroup-only run is an
explicit diagnostic mode and does not count as complete ClawTune telemetry.

## First Checks

Run these commands from the repository root and keep their output with any bug
report:

```bash
git rev-parse --short HEAD
uname -a
command -v python python3 pip claw-launch node npm openclaw
python -c 'import sys; print(sys.executable); print(sys.version)'
python -m pip --version
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
```

Run the strict preflight first when the API starts but managed executions fail:

```bash
export KERNEL_BUILD="$(readlink -f "/lib/modules/$(uname -r)/build")"

sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "$PWD/.venv-system/bin/python" tools/check_stage2.py
```

## Python and Package Problems

### `numpy` is installed, but `/usr/bin/python3` cannot import it

The package was installed for a different interpreter. A common example is:

```text
pip -> ~/miniconda3/envs/ML/lib/python3.12/site-packages
sudo /usr/bin/python3 -> system Python 3.11
```

`pip install numpy` in the active Conda environment does not install NumPy for
`/usr/bin/python3`. Confirm the mismatch with:

```bash
python -c 'import sys; print(sys.executable)'
python -m pip show numpy
/usr/bin/python3 -c 'import sys; print(sys.executable)'
```

For a diagnostic non-eBPF sidecar, one normal virtual environment is enough:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e "services/scheduler[dev]"
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

That mode is not the project default. For Stage-2, use the
[system-Python environment](#stage-2-ebpf-setup). Do not mix a Conda 3.12
process with a BCC extension built for Python 3.11.

### Importing `tool_resource.telemetry` asks for NumPy

Update the checkout. Current ClawTune lazily exports the public SDK, so the
independent telemetry submodule can be imported without eagerly loading the
NumPy-backed predictor stack. A full Scheduler still requires NumPy and the
rest of `services/scheduler/pyproject.toml`.

### `claw-launch` is not found

Install the Scheduler in the same environment that starts OpenClaw and use the
absolute path in plugin configuration:

```bash
source .venv-system/bin/activate
python -m pip install -e "services/scheduler[dev]"
command -v claw-launch
```

Then set plugin `launcherPath` to the printed path.

## Sidecar and Plugin Problems

### The sidecar does not become healthy

Start it in the foreground so its exception remains visible:

```bash
source .venv/bin/activate
python -m agent_scheduler.main --host 127.0.0.1 --port 8765
```

In another terminal:

```bash
curl -v http://127.0.0.1:8765/health/live
curl -v http://127.0.0.1:8765/health/ready
```

Check whether another process already owns the port:

```bash
ss -ltnp | grep ':8765'
```

### Plugin auto-start uses the wrong Python

Auto-start uses the Python visible to the OpenClaw process. This may differ
from the terminal where the Scheduler was installed. Disable auto-start and
start the sidecar manually while diagnosing:

```json5
{
  "autoStartSidecar": false
}
```

Alternatively, set an explicit trusted command after replacing the path:

```bash
export OPENCLAW_AGENT_SCHEDULER_SIDECAR_COMMAND="/absolute/path/to/.venv/bin/python -m agent_scheduler.main --host 127.0.0.1 --port 8765"
```

### No full LLM content or tool arguments

- The OpenClaw provider base URL must be `http://127.0.0.1:8765/v1`.
- Set plugin `recordRawTrace: true` if raw model/tool content is required.
- Do not put provider keys in committed configuration. The sidecar normally
  forwards OpenClaw's `Authorization` header.

## Cgroup Problems

### `cgroup_join_failed ... Permission denied` or launcher exit code 125

First confirm cgroup v2:

```bash
test -f /sys/fs/cgroup/cgroup.controllers \
  && echo 'cgroup v2 available' \
  || echo 'cgroup v2 unavailable'
```

For an explicit degraded diagnostic run, use process-tree attribution:

```json5
{
  "enableCgroup": false
}
```

Such a run does not satisfy strict telemetry. For temporary best-effort cgroup
diagnostics, set `enableCgroup: true` but keep `CLAW_CGROUP_REQUIRED=0`.

If strict cgroups are required, run OpenClaw inside a delegated systemd user
scope and compute the root inside that scope:

```bash
systemd-run --user --scope -p Delegate=yes bash -lc '
  set -euo pipefail
  self_cg="/sys/fs/cgroup$(awk -F: '\''$1=="0"{print $3}'\'' /proc/self/cgroup)"
  export CLAW_CGROUP_ROOT="$self_cg/claw"
  export CLAW_ENABLE_CGROUP=1
  export CLAW_CGROUP_REQUIRED=1
  exec openclaw agent --local --agent main --model "vllm/<your-model>" \
    --message "Run: python -c '\''print(\"cgroup-ok\")'\''"
'
```

Owning only the destination `cgroup.procs` file may still be insufficient:
cgroup v2 checks migration permissions through the source and destination
common ancestor.

## Stage-2 eBPF Setup

Stage-2 is the supported default. The sidecar and managed executions fail
closed unless clause-level CPU/RSS telemetry can start. SWE-Rebench
`host-openclaw-sandbox` is the maintained strict benchmark path.

### 1. Check host prerequisites

The current maintained path requires:

- Linux and effective UID 0 for the host runner;
- BCC/BPF Python bindings and their native libraries;
- Clang/LLVM and matching running-kernel development headers;
- cgroup v2;
- writable tracefs/kprobe controls;
- Docker for the OpenClaw sandbox and SWE-Rebench task containers.

Check the kernel tree:

```bash
export KERNEL_BUILD="$(readlink -f "/lib/modules/$(uname -r)/build")"
test -d "$KERNEL_BUILD" \
  && echo "kernel build tree: $KERNEL_BUILD" \
  || echo "matching kernel headers are missing"
```

Package names vary by distribution. Debian-family systems commonly expose the
binding as `bcc`; openEuler commonly exposes the same API as `bpfcc`.
ClawTune accepts either name.

### 2. Use a system-Python environment for BCC

When BCC is installed for `/usr/bin/python3`, create a virtual environment
that can see system packages, then install Scheduler dependencies into it:

```bash
/usr/bin/python3 -m venv --system-site-packages .venv-system
source .venv-system/bin/activate
python -m pip install --upgrade pip
python -m pip install -e "services/scheduler[dev]"
```

If `venv` is unavailable, install the distribution's Python venv package. Do
not create a repository-local `bcc` symlink: current ClawTune code imports
both `bcc` and `bpfcc` directly.

Verify the binding with the exact interpreter that will run Stage-2:

```bash
STAGE2_PY="$PWD/.venv-system/bin/python"

sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "$STAGE2_PY" - <<'PY'
import importlib

errors = []
for name in ("bcc", "bpfcc"):
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        errors.append(f"{name}: {type(exc).__name__}: {exc}")
        continue
    print("BCC binding:", module.__name__, module.__file__)
    print("BCC binding OK")
    break
else:
    raise SystemExit("; ".join(errors))
PY
```

### 3. Run the complete semantic preflight

The maintained one-command check compiles the full program, attaches kprobes
and perf sampling, creates a cgroup, runs a real exec, and rejects telemetry
loss or empty lifecycle data:

```bash
export KERNEL_BUILD="$(readlink -f "/lib/modules/$(uname -r)/build")"
STAGE2_PY="$PWD/.venv-system/bin/python"

sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "$STAGE2_PY" tools/check_stage2.py \
  --output /tmp/clawtune-stage2-preflight.json
```

Do not continue unless it exits `0` and reports `"stage2_ready": true`.

### 4. Isolate a BPF compile failure

If the semantic preflight fails during compilation, this narrower test extracts
`BPF_PROGRAM` without loading the predictor stack:

```bash
export KERNEL_BUILD="$(readlink -f "/lib/modules/$(uname -r)/build")"
STAGE2_PY="$PWD/.venv-system/bin/python"

sudo env \
  "PATH=/usr/sbin:/usr/bin:/sbin:/bin" \
  "BCC_KERNEL_SOURCE=$KERNEL_BUILD" \
  "$STAGE2_PY" - <<'PY'
import ast
import importlib
from pathlib import Path

for name in ("bcc", "bpfcc"):
    try:
        BPF = importlib.import_module(name).BPF
        break
    except (ImportError, AttributeError):
        pass
else:
    raise SystemExit("neither bcc nor bpfcc is importable")

source = Path("services/scheduler/src/tool_resource/telemetry.py")
tree = ast.parse(source.read_text(encoding="utf-8"))
assignment = next(
    node for node in tree.body
    if isinstance(node, ast.Assign)
    and any(
        isinstance(target, ast.Name) and target.id == "BPF_PROGRAM"
        for target in node.targets
    )
)
BPF(text=ast.literal_eval(assignment.value))
print("ClawTune BPF compile OK")
PY
```

`ClawTune BPF compile OK` confirms the binding, headers, compiler, and embedded
BPF source agree. It does not yet prove that runtime probes can attach or that
cgroups are writable.

### 5. Run one strict SWE-Rebench task

Configure the provider key and task list as described in the SWE-Rebench
guide, then run one host-sandbox task:

```bash
source .venv-system/bin/activate
export KERNEL_BUILD="$(readlink -f "/lib/modules/$(uname -r)/build")"

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

The runner repeats the semantic preflight and writes
`tool_resource_preflight_host.json` into the task trace directory. A strict run
must report `stage2_ready: true` before the task is released.

## BCC and Kernel-Specific Errors

### `ModuleNotFoundError: No module named 'bcc'`, but `bpfcc` exists

Do not create a symlink inside the repository. Verify the current checkout
contains `_BCC_BINDING_NAMES = ("bcc", "bpfcc")`:

```bash
grep -n '_BCC_BINDING_NAMES' services/scheduler/src/tool_resource/telemetry.py
/usr/bin/python3 -c 'import bpfcc; print(bpfcc.__file__)'
```

If the first command does not show both names, update the checkout before
continuing.

### Linux 6.2+ reports `mm->rss_stat.count[...]` compile errors

Linux 6.2 changed `mm_struct::rss_stat` from a wrapper with a `count` array to
an array of `struct percpu_counter`. Current ClawTune supports both layouts.
Confirm the compatibility macros exist:

```bash
grep -n 'CLAW_RSS_COUNTER_ADDR' services/scheduler/src/tool_resource/telemetry.py
```

If they are missing, the server is running an older checkout or stale generated
bundle. Update the checkout and rebuild with `--prepare`; do not manually edit
the installed kernel headers.

### `BCC_KERNEL_SOURCE` or kernel header failures

The build tree must match the running kernel exactly:

```bash
uname -r
readlink -f "/lib/modules/$(uname -r)/build"
```

Install the matching kernel development/header package for that exact release,
then set:

```bash
export BCC_KERNEL_SOURCE="$(readlink -f "/lib/modules/$(uname -r)/build")"
```

Reboot first if the installed kernel and running kernel differ.

### BPF compiles but Stage-2 still fails

Compilation does not attach kprobes, open perf events, discover Docker cgroups,
or run an actual command. Inspect these generated files in the failed task
trace directory:

- `tool_resource_preflight_host.json`
- `sidecar-stderr.txt`
- `sandbox-runtime-preflight.log`
- `report.json`

Typical causes are missing root privileges, read-only tracefs, unavailable
Docker inspection, cgroup v1, or failure to create an exclusive execution
cgroup.

## Filesystem Permissions

### Cannot create `swe_rebench/.runtime`

This usually means an earlier `sudo` run created the runtime directory as
root. Check the exact target before changing ownership:

```bash
runtime="$PWD/swe_rebench/.runtime"
printf 'runtime=%s\n' "$runtime"
ls -ld "$PWD" "$PWD/swe_rebench" "$runtime" 2>/dev/null || true
```

If that exact runtime directory is the only root-owned generated path, repair
it without changing ownership of the repository:

```bash
sudo install -d -o "$(id -u)" -g "$(id -g)" \
  "$PWD/swe_rebench/.runtime"
```

Do not run a recursive `chown` over an unspecified path. Git operations and
normal package installation should remain unprivileged; use `sudo` only for the
host-sandbox run that requires kernel access.

## ARM and Kunpeng

The Scheduler and plugin are architecture-neutral, and the telemetry code
recognizes arm64 syscall wrappers. Official SWE-Rebench task images are amd64,
so ARM/Kunpeng hosts need QEMU/binfmt and `linux/amd64` selection. Follow
[ARM/QEMU setup](arm-qemu.md) before preparing the runtime bundle.
