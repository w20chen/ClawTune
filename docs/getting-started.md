# Installation and First Run

This guide covers a fresh Linux checkout on Kunpeng/openEuler or x86_64 Linux.
See [configuration](configuration.md) for settings and
[troubleshooting](troubleshooting.md) when a check fails.

## Requirements

Use a normal login user with `sudo` access. The host needs Linux 5.8 or newer,
cgroup v2, and matching kernel development files. Confirm that site-managed
dependencies are already installed:

```bash
docker info
node --version
npm --version
openclaw --version
```

ClawTune supports Python 3.10 or newer and OpenClaw 2026.7.1 or newer. It does
not install or replace Docker, Node.js, or OpenClaw because their daemon,
repository, proxy, and security settings are site-specific.

## Install

From the repository root:

```bash
python3 scripts/clawtune.py setup
```

Do not prefix the whole command with `sudo`. Setup elevates only the operations
that need it. It:

- installs identifiable eBPF compiler, BCC, and kernel packages;
- creates the reusable `.venv` with access to the distribution BCC binding;
- installs the sidecar and builds/configures the OpenClaw plugin;
- builds the parser adapter for the privileged runtime;
- enables and tests amd64 Docker images on Kunpeng;
- compiles, attaches, and exercises the real eBPF collector;
- creates `.env` and `configs/benchmark.yaml` if absent.

Setup also keeps the older SWE/DRB config files for compatibility with their
internal runners. The supported public benchmark command uses
`configs/benchmark.yaml`.

A successful collector check includes:

```text
[ClawTune] Setup and eBPF validation passed; the validation process has exited.
```

The exited process is only the temporary validation. An eBPF validation
failure is non-fatal to installation but invalidates strict measurements.
Correct the host and rerun:

```bash
python3 scripts/clawtune.py check
```

## Configure the provider

### Benchmarks

Export the provider key in the shell that starts the run:

```bash
export LLM_API_KEY="<provider-api-key>"
```

Alternatively, put the raw key on one line in the ignored
`configs/llm_api_key.txt`. Edit `configs/benchmark.yaml`:

```yaml
llm:
  upstream_base_url: "https://api.deepseek.com"
  model: "your-model-name"
  openclaw_model_ref: "vllm/your-model-name"
```

Deep Research Bench also uses `TAVILY_API_KEY` when Tavily search is enabled.
See its [benchmark guide](../deep_research_bench/README.md).

### Normal OpenClaw runs

Configure an OpenAI-compatible provider with the proxy base URL
`http://127.0.0.1:8765/v1`. ClawTune forwards OpenClaw's authorization header
upstream. For a provider other than the default, set
`CLAWTUNE_LLM_UPSTREAM_BASE_URL` in `.env` and restart the sidecar.

## Start and verify

For an ongoing conversation:

```bash
# terminal 1
openclaw gateway run

# terminal 2
openclaw tui --session main
```

For a one-shot smoke test:

```bash
openclaw agent --local --agent main --model "vllm/<model>" \
  --message "Use the shell to run uname -a, then summarize it."
```

The plugin starts a compatible sidecar on demand and waits for readiness. A
pre-existing compatible sidecar is reused; another service on port 8765 is
rejected. Use the explicit sidecar command only when a service manager or a
non-interactive environment owns its lifetime:

```bash
python3 scripts/clawtune.py sidecar
```

Inspect the environment at any time:

```bash
python3 scripts/clawtune.py doctor
```

## Run a benchmark

List adapters and validate a small selection without Docker or an LLM:

```bash
python3 scripts/clawtune.py benchmark --list
python3 scripts/clawtune.py benchmark --sample 2 --dry-run
```

Then start with one live task:

```bash
python3 scripts/clawtune.py benchmark --benchmark swe-rebench --sample 1
```

The unified benchmark workflow uses `batch.parallelism` from the config or the
`--parallelism N` override. `1` is serial; larger values are the maximum number
of tasks in flight. Finished tasks are replaced immediately without a batch
barrier. Tool completions update the shared in-memory predictor and enqueue
coalesced persistence on one writer. Each task waits only for its own runtime
finalizers, and the run performs one global durability barrier before it
reports completion.

See the [adapter/input reference](MULTI_BENCHMARK_IMPLEMENTATION.md) and the
[SWE-Rebench guide](../swe_rebench/README.md).

Deep Research Bench uses the same runner:

```bash
TAVILY_API_KEY="<key>" python3 scripts/clawtune.py benchmark \
  --benchmark deep-research-bench --sample 1
```

`python3 scripts/clawtune.py drb ...` is a compatibility alias for the same
command.

## Updating the checkout

After pulling commits or moving the repository, rerun:

```bash
python3 scripts/clawtune.py setup
```

Setup preserves existing secrets/configuration, refreshes the editable
sidecar/plugin installation and trusted launcher path, validates the OpenClaw
schema, and checks eBPF again.
