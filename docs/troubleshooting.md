# Troubleshooting

Start with one report instead of trying interpreter and environment commands at
random:

```bash
python3 scripts/clawtune.py doctor
```

Then rerun setup. It is safe to run after a partial installation or repository
update:

```bash
python3 scripts/clawtune.py setup
```

If it still fails, match the final error to a section below. Include the
`doctor` output, `uname -a`, and `git rev-parse --short HEAD` in a bug report.

## `apt-get: command not found`

This is normal on openEuler, EulerOS, RHEL, and related distributions. They use
`dnf`. Do not paste the Debian/Ubuntu package command. The unified setup detects
the available package manager:

```bash
python3 scripts/clawtune.py setup
```

If it says the dnf repositories do not contain BCC or `kernel-devel`, confirm
that the OS and update repositories for the running openEuler release are
enabled. A custom or vendor kernel must provide a matching development package.

## BCC is installed but Python cannot import it

The usual cause is two different Python installations:

```text
pip / python       -> Conda environment
/usr/bin/python3   -> distribution BCC package
```

Installing NumPy in Conda does not add it to `/usr/bin/python3`, and installing
BCC for the system interpreter does not add it to Conda. ClawTune solves this
by creating `.venv` from the system Python with access to system packages, then
installing the Scheduler's Python dependencies into that same environment.

Run setup as your normal account, even if the prompt shows an active Conda
environment. Do not create a local `bcc` symlink or compatibility
`PYTHONPATH` directory. Both upstream import names are supported:

- `bcc` on Debian/Ubuntu and many other distributions;
- `bpfcc` on some openEuler installations.

If `.venv` was previously created from the wrong interpreter, rename it for
inspection or delete it if you no longer need it, then rerun setup. Setup never
deletes it automatically.

## `.venv/bin/python: No such file or directory`

The environment has not been created in this checkout, or an older guide used
a different directory name. The supported environment is now only `.venv`:

```bash
python3 scripts/clawtune.py setup
```

Do not manually activate the environment for normal operation. The unified
commands use its absolute interpreter path.

## Matching kernel headers are missing

`doctor` prints the expected path, normally:

```text
/lib/modules/<running-kernel>/build
```

Reboot if the package manager installed a new kernel but the host is still
running the old one. For a custom kernel, install/build the development tree
for the exact output of `uname -r` and make the `build` link resolve to it.
Generic headers for another kernel version are not sufficient.

## The basic BPF example compiles but ClawTune does not

A one-line BPF program only proves that Clang and BCC can run. Use the complete
project check:

```bash
python3 scripts/clawtune.py check
```

It also attaches probes, exercises the cgroup/process path, and rejects missing
events. If an error references `mm_struct.rss_stat.count` on Linux 6.2 or newer,
the checkout is old: update ClawTune and rerun setup. Current code supports the
array layout used by newer kernels as well as the older layout.

## eBPF check reports permission, tracefs, perf, or cgroup errors

Use the wrapper rather than invoking `tools/check_ebpf.py` directly; it supplies
the verified interpreter, matching kernel path, clean executable path, and
sudo. Confirm these host properties:

```bash
test -f /sys/fs/cgroup/cgroup.controllers && echo "cgroup v2: yes"
mount | grep -E 'tracefs|debugfs'
docker info
```

Hardened kernels may deny BPF or perf even to containers. The maintained path
runs the sidecar on the host as root and lets OpenClaw execute tools in Docker.
A remote Docker daemon cannot use the local kernel collector.

## `npm run build` reports `TS5033 ... EACCES` under `dist`

An older privileged prepare step may have created plugin output as root. Setup
repairs this focused directory automatically. If a manual repair is needed:

```bash
sudo chown -R "$(id -u):$(id -g)" packages/openclaw-plugin/dist
python3 scripts/clawtune.py setup
```

Do not recursively change ownership of the repository.

## OpenClaw, Docker, Node.js, or npm is missing

Setup lists all missing external applications in one message. Install them
using your organization's supported repository and daemon configuration, make
sure each command works as your normal account, and rerun setup. In particular,
adding the user to Docker's group may require a new login session.

## Sidecar does not start or port 8765 is already in use

The foreground command keeps the real error visible:

```bash
python3 scripts/clawtune.py sidecar
```

Check the port and health endpoints in another terminal:

```bash
ss -ltnp | grep ':8765'
curl -v http://127.0.0.1:8765/health/live
curl -v http://127.0.0.1:8765/health/ready
```

Stop the older sidecar or change the port consistently in `.env`, the plugin,
and provider proxy URL.

### OpenClaw reports `ECONNREFUSED 127.0.0.1:8765`

Provider onboarding succeeded, but the local ClawTune proxy is not running.
The eBPF validation performed by setup is temporary and does not start a
background service. Rerun the agent through the automatic wrapper:

```bash
cd ~/ClawTune
python3 scripts/clawtune.py agent \
  --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'."
```

It starts the privileged sidecar, waits for readiness, and only then starts
OpenClaw. For a long-lived sidecar, keep this command open in one terminal:

```bash
python3 scripts/clawtune.py sidecar
```

Then verify/run OpenClaw from a second terminal:

```bash
curl -fsS http://127.0.0.1:8765/health/ready
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'."
```

Repeated `feishu` state-migration warnings are independent of this connection
failure. Resolve them separately only if that plugin is in scope.

## OpenClaw runs but model or tool traces are empty

Check that:

- the sidecar is running before OpenClaw starts work;
- the OpenClaw provider base URL is `http://127.0.0.1:8765/v1`;
- the plugin is enabled with `openclaw plugins list`;
- setup configured the launcher under the current checkout's `.venv`;
- the provider key/model names are correct.

Rerunning setup refreshes the plugin link and absolute launcher path after a
checkout has moved.

## OpenClaw reports `plugins.load.paths: plugin path not found`

The OpenClaw config contains a linked plugin path from an older checkout, for
example `/home/user/claw/...` after the repository moved to
`/home/user/ClawTune/...`. Current setup recognizes a missing ClawTune plugin
link, backs up the config, runs `openclaw doctor --fix`, then installs the link
from the current checkout:

```bash
python3 scripts/clawtune.py setup
```

The backup is written next to `~/.openclaw/openclaw.json` with a timestamp. If
an older checkout does not yet contain this recovery, run `openclaw doctor
--fix` once and then rerun setup.

## Kunpeng cannot run an amd64 image

Rerun setup; it installs and tests the binfmt handler on arm64. For a focused
test:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh check
```

If Docker cannot pull `tonistiigi/binfmt` or the smoke image, fix registry,
proxy, or DNS access first. See [Kunpeng and arm64](arm-qemu.md).

## Benchmark fails or produces no final report

Look in the task directory under `swe_rebench/.runtime/traces/<task-id>/`:

- `tool_resource_preflight_host.json`: kernel collector checks;
- `sidecar-stderr.txt`: sidecar/BCC errors;
- `openclaw-stderr.txt`: provider and agent errors;
- `sandbox-runtime-preflight.log`: task Python and pip selection;
- `report.json`: batch summary and separated agent/telemetry diagnostics.

Start with one task. On Kunpeng, increase `batch.task_timeout_seconds` only if
QEMU execution genuinely reaches the current limit.
