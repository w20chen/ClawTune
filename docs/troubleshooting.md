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

## Table of Contents

- [1. `apt-get: command not found`](#1-apt-get-command-not-found)
- [2. Container stops at `checking container system dependencies`](#2-container-stops-at-checking-container-system-dependencies)
- [3. BCC is installed but Python cannot import it](#3-bcc-is-installed-but-python-cannot-import-it)
- [4. `.venv/bin/python: No such file or directory`](#4-venvbinpython-no-such-file-or-directory)
- [5. Matching kernel headers are missing](#5-matching-kernel-headers-are-missing)
- [6. The basic BPF example compiles but ClawTune does not](#6-the-basic-bpf-example-compiles-but-clawtune-does-not)
- [7. eBPF check reports permission, tracefs, perf, or cgroup errors](#7-ebpf-check-reports-permission-tracefs-perf-or-cgroup-errors)
- [8. `npm run build` reports `TS5033 ... EACCES` under `dist`](#8-npm-run-build-reports-ts5033--eacces-under-dist)
- [9. OpenClaw, Docker, Node.js, or npm is missing](#9-openclaw-docker-nodejs-or-npm-is-missing)
- [10. Sidecar does not start or port 8765 is already in use](#10-sidecar-does-not-start-or-port-8765-is-already-in-use)
- [11. OpenClaw reports `ECONNREFUSED 127.0.0.1:8765`](#11-openclaw-reports-econnrefused-1270018765)
- [12. OpenClaw warns that `plugins.allow` is empty](#12-openclaw-warns-that-pluginsallow-is-empty)
- [13. Tool output contains `Failed to connect to bus: No medium found`](#13-tool-output-contains-failed-to-connect-to-bus-no-medium-found)
- [14. OpenClaw runs but model or tool traces are empty](#14-openclaw-runs-but-model-or-tool-traces-are-empty)
- [15. Benchmark cannot find `LLM_API_KEY`](#15-benchmark-cannot-find-llm_api_key)
- [16. OpenClaw rejects `agents.defaults.sandbox.docker.platform`](#16-openclaw-rejects-agentsdefaultssandboxdockerplatform)
- [17. OpenClaw reports `plugins.load.paths: plugin path not found`](#17-openclaw-reports-pluginsloadpaths-plugin-path-not-found)
- [18. Kunpeng/ARM cannot run an amd64 image](#18-kunpengarm-cannot-run-an-amd64-image)
- [19. Benchmark fails or produces no final report](#19-benchmark-fails-or-produces-no-final-report)
- [20. A trace reports `mvdan adapter is missing` or repeated `analysis_failure`](#20-a-trace-reports-mvdan-adapter-is-missing-or-repeated-analysis_failure)
- [21. Replay failures](#21-replay-failures)
- [22. Standalone `web_search` uses DuckDuckGo or fails instead of Tavily](#22-standalone-web_search-uses-duckduckgo-or-fails-instead-of-tavily)

## 1. `apt-get: command not found`

This is normal on openEuler, EulerOS, RHEL, and related distributions. They use
`dnf`. Do not paste the Debian/Ubuntu package command. The unified setup detects
the available package manager:

```bash
python3 scripts/clawtune.py setup
```

If it says the dnf repositories do not contain BCC or `kernel-devel`, confirm
that the OS and update repositories for the running openEuler release are
enabled. A custom or vendor kernel must provide a matching development package.

## 2. Container stops at `checking container system dependencies`

This message belongs only to the `container-openclaw` runtime. Its first run
may need to install packages inside the benchmark image; running an amd64 image
through QEMU on Kunpeng is slower than native execution. Current builds print
the detected package manager and package-download progress. If container DNS,
proxy, or repository access is broken, setup exits with an explicit error
after bounded retries instead of waiting silently.

Seeing `container arch: x86_64` on an arm64 Kunpeng host is expected for an
x86_64 SWE-Rebench image. Re-run the same benchmark after fixing the repository
or network error shown in the log; no separate runtime configuration is needed.

## 3. BCC is installed but Python cannot import it

The usual cause is two different Python installations:

```text
pip / python       -> Conda environment
/usr/bin/python3   -> distribution BCC package
```

Installing NumPy in Conda does not add it to `/usr/bin/python3`, and installing
BCC for the system interpreter does not add it to Conda. ClawTune solves this
by creating `.venv` from the system Python with access to system packages, then
installing the Sidecar's Python dependencies into that same environment.

Run setup as your normal account, even if the prompt shows an active Conda
environment. Do not create a local `bcc` symlink or compatibility
`PYTHONPATH` directory. Both upstream import names are supported:

- `bcc` on Debian/Ubuntu and many other distributions;
- `bpfcc` on some openEuler installations.

If `.venv` was previously created from the wrong interpreter, rename it for
inspection or delete it if you no longer need it, then rerun setup. Setup never
deletes it automatically.

## 4. `.venv/bin/python: No such file or directory`

The environment has not been created in this checkout, or an older guide used
a different directory name. The supported environment is now only `.venv`:

```bash
python3 scripts/clawtune.py setup
```

Do not manually activate the environment for normal operation. The unified
commands use its absolute interpreter path.

## 5. Matching kernel headers are missing

`doctor` prints the expected path, normally:

```text
/lib/modules/<running-kernel>/build
```

Reboot if the package manager installed a new kernel but the host is still
running the old one. For a custom kernel, install/build the development tree
for the exact output of `uname -r` and make the `build` link resolve to it.
Generic headers for another kernel version are not sufficient.

ClawTune's supported baseline is Linux 5.8 or newer with cgroup v2. A newer
header package cannot make an older running kernel compatible; update and boot
the kernel first, then install the development package for that exact release.

## 6. The basic BPF example compiles but ClawTune does not

A one-line BPF program only proves that Clang and BCC can run. Use the complete
project check:

```bash
python3 scripts/clawtune.py check
```

It also attaches probes, exercises the cgroup/process path, and rejects missing
events. If an error references `mm_struct.rss_stat.count` on Linux 6.2 or newer,
the checkout is old: update ClawTune and rerun setup. Current code supports the
array layout used by newer kernels as well as the older wrapped layout. It
selects the access expression from the actual `mm_struct.rss_stat` type exposed
by the running kernel's matching headers rather than from a hard-coded kernel
version.

Likewise, syscall kprobe names differ between `x86_64`, `aarch64`, and vendor
kernels. ClawTune tries architecture-appropriate symbol candidates and uses
the first attachable one. BCC may print `probe entry may not exist` while a
candidate is being tested. If the final `check` reports success, those
individual candidate messages are not a failure; if all candidates fail, keep
the complete output in the bug report.

## 7. eBPF check reports permission, tracefs, perf, or cgroup errors

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

## 8. `npm run build` reports `TS5033 ... EACCES` under `dist`

An older privileged prepare step may have created plugin output as root. Setup
repairs this focused directory automatically. If a manual repair is needed:

```bash
sudo chown -R "$(id -u):$(id -g)" packages/clawtune-plugin/dist
python3 scripts/clawtune.py setup
```

Do not recursively change ownership of the repository.

## 9. OpenClaw, Docker, Node.js, or npm is missing

Setup lists all missing external applications in one message. Install them
using your organization's supported repository and daemon configuration, make
sure each command works as your normal account, and rerun setup. In particular,
adding the user to Docker's group may require a new login session.

## 10. Sidecar does not start or port 8765 is already in use

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

The JSON must identify `service` as `clawtune-sidecar` and `schema_version`
as `clawtune.health.v1`. A `200` response from an unrelated program is not
accepted. Stop the conflicting process before rerunning ClawTune; port 8765 is
the supported setup default.

## 11. OpenClaw reports `ECONNREFUSED 127.0.0.1:8765`

Provider onboarding succeeded, but the local ClawTune proxy is not running.
The eBPF validation performed by setup is temporary. Current setup configures
the plugin to auto-start the privileged sidecar and wait before the first model
request. After updating the checkout, rerun setup once so it rebuilds and
validates the plugin configuration:

```bash
cd ~/ClawTune
python3 scripts/clawtune.py setup --skip-qemu
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'."
```

For a non-interactive environment where sudo cannot prompt, use the explicit
Python `agent` wrapper or keep a long-lived sidecar open in one terminal:

```bash
python3 scripts/clawtune.py agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'."
# or
python3 scripts/clawtune.py sidecar
```

Then verify/run OpenClaw from a second terminal:

```bash
curl -fsS http://127.0.0.1:8765/health/ready
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run: python -c 'print(\"clawtune-ok\")'."
```

Repeated `feishu` state-migration warnings are independent of this connection
failure. They describe conflicting legacy OpenClaw plugin-install metadata,
not ClawTune or eBPF. If the agent run succeeds, do not edit OpenClaw's SQLite
state manually. Run `openclaw doctor --fix`; if Feishu is not used, inspect
`openclaw plugins uninstall feishu --dry-run` before deciding whether to
remove it.

## 12. OpenClaw warns that `plugins.allow` is empty

This is a plugin trust warning, not a sidecar failure. Inspect every plugin
before creating an allowlist:

```bash
openclaw plugins list --enabled --verbose
openclaw plugins inspect clawtune --json
openclaw plugins inspect deepseek --json
openclaw plugins inspect feishu --json
```

`plugins.allow` is exclusive. Include `clawtune` and every other plugin
that is both trusted and needed. For a host that uses only ClawTune with the
built-in/custom `vllm` provider, for example:

```bash
openclaw config set plugins.allow '["clawtune"]' --strict-json
openclaw config validate
```

Add `deepseek`, `feishu`, or other inspected IDs only when those plugins are
actually required. Omitting `clawtune` would prevent ClawTune from
loading.

If OpenClaw instead reports that the external plugin cannot register the
protected `agent_end` conversation hook, the installed configuration predates
the required per-plugin permission. Update the checkout, rerun setup, and
restart the Gateway:

```bash
python3 scripts/clawtune.py setup --skip-qemu
openclaw config get plugins.entries.clawtune.hooks
openclaw config validate
openclaw gateway restart
```

The first config command must show `allowConversationAccess: true`. This is a
sibling of the plugin's `config` object; placing it inside `config` does not
grant the OpenClaw hook permission.

## 13. Tool output contains `Failed to connect to bus: No medium found`

The launcher probes the systemd user manager quietly before using it. When no
systemd user manager is available (for example in an SSH session), it keeps the
gated payload, removes the unused cgroup, and reports no false cgroup path to
the eBPF sidecar. If the message persists, update the checkout and rerun setup
so OpenClaw uses the rebuilt launcher:

```bash
python3 scripts/clawtune.py setup --skip-qemu
```

If `collector attach failed` mentions a missing `/sys/fs/cgroup/.../cpu.max`,
update ClawTune and rerun setup. The CPU controller is optional for eBPF
collection; a missing quota file is interpreted as unconstrained host capacity
instead of a shell-execution failure. You do not need to modify
`cgroup.subtree_control` manually.

## 14. OpenClaw runs but model or tool traces are empty

Check that:

- the sidecar is running before OpenClaw starts work;
- the OpenClaw provider base URL is `http://127.0.0.1:8765/v1`;
- the plugin is enabled with `openclaw plugins list`;
- setup configured the launcher under the current checkout's `.venv`;
- the provider key/model names are correct.

Rerunning setup refreshes the plugin link and absolute launcher path after a
checkout has moved.

## 15. Benchmark cannot find `LLM_API_KEY`

Export the key in the same shell that invokes the unified command:

```bash
export LLM_API_KEY="<provider-api-key>"
python3 scripts/clawtune.py benchmark --sample 1
```

The wrapper narrowly preserves `LLM_API_KEY` across its privileged boundary;
do not replace it with `sudo -E`. If site policy forbids preserving that
variable, put the key on one line in the Git-ignored
`swe_rebench/llm_api_key.txt` (or `deep_research_bench/llm_api_key.txt` for
Deep Research Bench), or export `LLM_API_KEY_FILE` with the path to a
site-managed secret file.

## 16. OpenClaw rejects `agents.defaults.sandbox.docker.platform`

That key is not part of the OpenClaw 2026.7.x configuration schema. Remove it
from hand-written OpenClaw JSON and rerun setup:

```bash
python3 scripts/clawtune.py setup
openclaw config validate
```

ClawTune communicates the selected architecture through its Docker operations
and child environment. On Kunpeng the benchmark wrapper defaults to
`linux/amd64`; an explicit `SWE_REBENCH_DOCKER_PLATFORM` value takes priority.
On x86 the default is native.

## 17. OpenClaw reports `plugins.load.paths: plugin path not found`

The OpenClaw config contains a linked plugin path that no longer exists, for
example `/home/user/clawtune/...` after the repository moved to
`/home/user/ClawTune/...`. Setup recognizes the missing ClawTune plugin link,
backs up the config, and removes the missing `clawtune-plugin` entry from
`plugins.load.paths`. If the stale reference lives in OpenClaw's internal
plugin state rather than in `plugins.load.paths`, setup falls back to
`openclaw doctor --fix` to reconcile the internal registry, then removes any
restored stale paths before installing the link from the current checkout:

```bash
python3 scripts/clawtune.py setup
```

The backup is written next to `~/.openclaw/openclaw.json` with a timestamp.
Other plugin paths are preserved. If setup cannot remove a stale reference,
remove the missing path from `plugins.load.paths` manually, run
`openclaw doctor --fix`, and then rerun setup.

## 18. Kunpeng/ARM cannot run an amd64 image

### Symptom

Benchmark tasks fail immediately with errors like:

```
exec /bin/sh: exec format error
libcontainer: container start initialization failed
```

or:

```
sandbox_launcher_preflight_failed: the mounted clawtune-launch must be readable
and select a supported fork-exec runtime in the sandbox
```

When you check with the smoke test:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh check
```

it reports that binfmt_misc is not registered for x86_64 binaries.

### Explanation

ARM (aarch64 / Kunpeng) hosts run SWE-Rebench x86_64 Docker images through
QEMU user-mode emulation.  This requires the Linux kernel's `binfmt_misc`
mechanism to register a handler that transparently runs x86_64 binaries via
`qemu-x86_64-static`.

The `clawtune.py setup` command runs this registration automatically on ARM
hosts via `scripts/setup/arm_qemu_setup.sh install`.  However, **binfmt_misc
registrations do not survive a reboot.**  After the host restarts, Docker
cannot execute x86_64 container binaries, producing `exec format error`.

### Fix

Re-register the QEMU binfmt handler (no need to re-run the full setup):

```bash
sudo bash scripts/setup/arm_qemu_setup.sh install
```

Verify it works:

```bash
sudo bash scripts/setup/arm_qemu_setup.sh check
```

The check output should show `binfmt_misc` registered and an amd64 smoke
container running successfully.  If Docker cannot pull `tonistiigi/binfmt` or
the smoke image, fix registry, proxy, or DNS access first.
See [Kunpeng and arm64](arm-qemu.md).

For benchmark task images, `pull_policy: missing` first checks the local image
and verifies its requested OS/architecture. A matching cached amd64 image is
used directly on Kunpeng even when Docker Hub is temporarily unreachable.
Only an absent or wrong-architecture image requires registry access.

## 19. Benchmark fails or produces no final report

Look in the task directory under `swe_rebench/.runtime/traces/<task-id>/`:

- `tool_resource_preflight_host.json`: kernel collector checks;
- `sidecar-stderr.txt`: sidecar/BCC errors;
- `openclaw-stderr.txt`: provider and agent errors;
- `sandbox-runtime-preflight.log`: task Python and pip selection;
- `report.json`: batch summary and separated agent/telemetry diagnostics.

Start with one task. On Kunpeng, increase `batch.task_timeout_seconds` only if
QEMU execution genuinely reaches the current limit.

For Deep Research Bench, the task directory is
`deep_research_bench/.runtime/traces/<task-id>/` and the batch report is
`deep_research_bench/.runtime/report.json`. If the relaxed telemetry gate fails
with `required resource telemetry found no tool spans`, the agent answered
without calling any instrumented tool. For Tavily web search, confirm
`TAVILY_API_KEY` (or `deep_research_bench/tavily_api_key.txt`) is configured
and reaches the runner through the `sudo` allow-list — `web_search` runs on the
host, so the key does not need to reach the sandbox image. Otherwise check the
sandbox image's network and the OpenClaw binary's built-in web tools, or use
`--no-gate-required` for a best-effort run. If the basic sandbox image cannot
be pulled, check `pull_policy` and registry access for `sandbox.image`.

If a DRB task fails during agent setup with
`openclaw_web_search_config_patch_failed` and
`tools.web.search.provider: ... provider is not available: tavily`, the host's
OpenClaw does not have the `tavily` plugin visible in the task's isolated
OpenClaw home. The runner first tries to link a globally installed `tavily`
plugin (found under `~/.openclaw/npm/projects/openclaw-tavily-plugin-*`) into
that home so web search can actually use it; if that is not possible it
degrades to auto-detection instead of failing the whole task. To pin Tavily
deterministically, install the plugin on the host:

```bash
openclaw plugin install tavily
openclaw doctor --fix
```

The auto-link / degradation notes and this hint are recorded in
`deep_research_bench/.runtime/traces/<task-id>/web-search-config.log`.

## 20. A trace reports `mvdan adapter is missing` or repeated `analysis_failure`

The shell-clause parser adapter is built per user and per architecture. If a
trace reports `mvdan adapter is missing`, the adapter cache was prepared under
a different user or architecture than the sidecar uses. Rerun setup so it
prepares the adapter as the actual sidecar identity; benchmark preflight
verifies it before starting an agent. Do not copy binaries between users or
architectures:

```bash
python3 scripts/clawtune.py setup
```


## 21. Replay failures

SWE-Rebench replay currently supports only a current-format JSONL trace and the
`host-openclaw` runtime. It intentionally reuses the normal task-image
export, OpenClaw sandbox, task environment, launcher, sidecar, cgroup, and
eBPF path; it does not execute tools directly on the host.

If replay rejects a trace, inspect `replay_error.txt`. Common causes are an
older-format `action` trace, an incomplete LLM span, or a tool span whose
`input.requested_args` was redacted or truncated. These cases are fail-closed
because reconstructing a command from a prediction or launcher wrapper would
be unsafe. If the replay has no resource artifact, inspect
`tool_resource_preflight_host.json`, `sidecar-stderr.txt`, and
  `replay_manifest.json`; the same Linux, Docker, cgroup v2, BCC/eBPF, and
privilege requirements as a normal host-openclaw benchmark apply.

Before troubleshooting the runtime, verify that the task dataset and source
trace identify the same case. The dataset supplies the Docker image and the
trace supplies the recorded interaction; a trace alone cannot recreate the
SWE-Rebench filesystem or installed dependencies. Replay output is stored in
`swe_rebench/replays/<task-id>/`.

Typical commands and their causes:

- `task id ... was not found uniquely`: pass the dataset containing the exact
  `--task-id`, or use the correct instance ID from the trace directory.
- `trace ... is not the current format`: the source is an older-format/action
  trace; first export or collect a current ClawTune trace.
- `has no replayable requested arguments`: raw tool arguments were disabled,
  redacted, or truncated. Replay does not infer commands from predictions or
  launcher wrappers.
- no new JSONL or exec-clause artifact: inspect `phase3.log`,
  `launcher-preflight.log`, `tool_resource_preflight_host.json`, and
  `sidecar-stderr.txt` in the replay directory. The replay needs the same
  Linux host privileges and eBPF readiness as a normal benchmark.

Replay uses a separate workspace and does not modify the source trace. If a
replay command is unsafe or unexpected, stop the run and remove the replay
workspace and `swe_rebench/replays/<task-id>/` artifacts after collecting the
diagnostic logs.

## 22. Standalone `web_search` uses DuckDuckGo or fails instead of Tavily

The DRB harness pins `tools.web.search.provider: tavily` inside each task's
isolated OpenClaw home automatically. Outside a benchmark (your own
`openclaw agent` / `~/.openclaw`), `web_search` auto-detects the provider and
can pick DuckDuckGo instead of Tavily. On restricted hosts DuckDuckGo is
unreachable, so `web_search` fails with either:

- `[fetch-timeout] fetch timeout after 20000ms ... url=https://html.duckduckgo.com/html`; or
- `[security] blocked URL fetch ... reason=Blocked: resolves to private/internal/special-use IP address`.

Pin Tavily and provide a key:

```bash
openclaw config set tools.web.search.provider tavily
export TAVILY_API_KEY="<key>"   # or: openclaw config set plugins.entries.tavily.config.webSearch.apiKey "<key>"
```

Related standalone gotchas:

- `openclaw agent --local` embeds its own gateway; do not run
  `openclaw gateway restart` (no systemd service here — it is a no-op that can
  leave stale pid state; see also sections 10-11 for the sidecar/port 8765
  failure mode).
- ClawTune Sidecar auto-start needs root. If it times out after 60s
  with empty stderr, refresh sudo with `sudo -v` and re-run; the failure then
  surfaces as `LLM request failed: network connection error` /
  `ECONNREFUSED` on `127.0.0.1:8765`.
- To confirm which provider `web_search` resolves to, check the run log for the
  `url=` of the fetch operation (`html.duckduckgo.com` = DuckDuckGo,
  `api.tavily.com` = Tavily).
