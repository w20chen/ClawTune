# Installation and first run

This guide expands the short path in the root README. It is written for a
fresh Linux checkout and covers both Kunpeng/openEuler and x86_64 Linux.

## Before you begin

Use a normal login user with `sudo` access. Confirm that Docker, Node.js/npm,
and OpenClaw are installed and usable by that user:

```bash
docker info
node --version
npm --version
openclaw --version
```

ClawTune does not install or replace these three applications because their
repository, daemon, proxy, and security settings are site-specific. Everything
kernel/eBPF-related is handled by the project setup command on systems using
`dnf` or `apt`.

Docker must use a Linux daemon. The running kernel must provide cgroup v2 and
matching development files must be available in the distribution repository.

## Install ClawTune

From the repository root:

```bash
python3 scripts/clawtune.py setup
```

Do not prefix the whole command with `sudo`. The script elevates only package,
QEMU, ownership-repair, and eBPF validation operations. Running the complete
setup as root would create files that your normal account cannot rebuild.

The setup command intentionally ignores the active Conda interpreter when it
does not own BCC. On openEuler it can use the distribution module named
`bpfcc`; on Debian/Ubuntu it can use `bcc`. Both expose the API ClawTune needs.
No import symlink and no `PYTHONPATH` compatibility directory are required.

The reusable environment is always `.venv`. If that directory already exists
but was created from the wrong Python, setup asks you to rename or remove that
single directory rather than attempting to combine incompatible interpreters.

At the end, setup runs a real validation: compile the complete BPF program,
attach its probes, create a test cgroup, execute a process, and verify usable
events. The report is saved at `data/ebpf-check.json`.

## Configure a provider

### Benchmark runs

Put the raw API key on one line in:

```text
swe_rebench/llm_api_key.txt
```

Edit `swe_rebench/config.yaml` and set the upstream URL and model names. Most
users do not need to change the runtime, Docker privilege, cgroup, bundle, or
output sections. On arm64, the command-line wrapper selects the required Docker
platform automatically.

### Normal OpenClaw runs

The plugin is installed and enabled by setup. It expects the sidecar at
`http://127.0.0.1:8765` and uses the absolute launcher installed in `.venv`.
Configure the OpenClaw model provider with proxy base URL:

```text
http://127.0.0.1:8765/v1
```

ClawTune forwards the authorization header to the upstream provider. If the
upstream URL is not DeepSeek-compatible, set
`AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL` in `.env` and restart the sidecar.

## Start and verify

Run the agent through ClawTune's wrapper:

```bash
python3 scripts/clawtune.py agent \
  --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run uname -a, then summarize it."
```

It asks for sudo when needed, starts the eBPF sidecar with the verified `.venv`
and kernel environment, waits for port 8765, and then starts OpenClaw. It stops
only the sidecar it created; a pre-existing sidecar is reused and retained.

For a gateway or multiple agent commands, use the long-lived form in a separate
terminal:

```bash
python3 scripts/clawtune.py sidecar
```

Use the consolidated environment report at any time:

```bash
python3 scripts/clawtune.py doctor
```

Repeat the kernel-level collector test after a kernel/BCC update:

```bash
python3 scripts/clawtune.py check
```

## Run SWE-Rebench

With the usual sibling `agent-test-bench` task checkout:

```bash
python3 scripts/clawtune.py benchmark --sample 1
```

Or name a task source:

```bash
python3 scripts/clawtune.py benchmark \
  --dataset /path/to/tasks.json \
  --instance-ids django__django-12345
```

Start with one task. QEMU runs on Kunpeng are slower than native x86_64 runs,
so increase `batch.task_timeout_seconds` in the benchmark config only when a
real task reaches the default deadline.

## Updating the checkout

After pulling commits, rerun the same idempotent setup command:

```bash
python3 scripts/clawtune.py setup
```

It rebuilds the plugin, refreshes the editable Scheduler installation, retains
your existing `.env` and benchmark config, and verifies eBPF again.

If the repository moved or only its path capitalization changed, setup detects
an invalid old ClawTune link before plugin installation. It backs up
`~/.openclaw/openclaw.json`, runs OpenClaw's repair command, and links the
current checkout. Other valid plugin paths are retained by OpenClaw.
