# ClawTune

ClawTune connects OpenClaw to a local sidecar that records model/tool traces,
measures Linux process and eBPF resource usage, and learns duration, CPU, memory
and PMU predictions. Placement advice is advisory; this project does not modify
OpenClaw core.

| Workflow | Command | Knowledge base |
| --- | --- | --- |
| Daily OpenClaw use | `openclaw gateway run` | Persistent user KB |
| Online benchmark simulation | `python3 scripts/clawtune.py benchmark ...` | New writable KB per run |
| Fixed-trace evaluation | `python3 scripts/clawtune.py offline ...` | Train by task, freeze during testing |

## First run

From a checkout on x86_64 Linux or Kunpeng/arm64 openEuler, with Python 3.10+,
Docker, Node.js/npm, OpenClaw 2026.7.1+, cgroup v2 and matching kernel headers:

```bash
python3 scripts/clawtune.py setup
python3 scripts/clawtune.py doctor
```

Run setup as a normal user with sudo access. It creates `.venv`, `.env` and
`configs/benchmark.yaml`, installs the sidecar/plugin, and tests the collector.
It preserves existing configuration and secrets. Follow the
[installation guide](docs/getting-started.md) for prerequisites, provider setup,
Gateway/TUI use, and verification. Windows supports development and dry-runs;
live collection requires Linux.

For a first benchmark, put your provider key in the ignored
`configs/llm_api_key.txt` or export it, then set `llm.model`,
`llm.openclaw_model_ref` and `llm.upstream_base_url` in `configs/benchmark.yaml`:

```bash
export LLM_API_KEY="<provider-api-key>"
python3 scripts/clawtune.py benchmark --list
python3 scripts/clawtune.py benchmark --sample 1 --dry-run
python3 scripts/clawtune.py benchmark --sample 1
```

The default source is the prepared external SWE-Rebench task list if present,
otherwise the bundled smoke list. Use an explicit `--dataset` for experiments.
A dry-run validates task input and seed, not Docker images, provider access or
live telemetry.

## Benchmarks

These are instrumented user simulations, **not official leaderboard graders**.
`official_score` is always `null`; successful execution does not prove a solved task.

| `--benchmark` | Supported input/execution |
| --- | --- |
| `swe-rebench` | Repository tasks with dataset-provided Docker images |
| `swe-bench-verified` | Repository tasks; official x86_64 image name inferred if absent |
| `deep-research-bench` | Research questions, OpenClaw web tools and a basic sandbox |
| `bfcl` | Independent stateful function tasks, including base/long-context and web search; no memory dependencies or dynamic tool additions |
| `terminal-bench` | v1 `task.yaml` tasks with Compose or a Dockerfile; no Harbor/v2 task format or persistent TTY |

[Benchmark guide](docs/benchmarks.md): exact paths, dependencies, examples,
selection, timeouts, concurrency, resume rules and output checks.

## Offline evaluation

```bash
python3 scripts/clawtune.py offline --dataset /data/fixed-traces --rss-unit MiB
```

The input is a directory of v5/v6 **traces**, not online task JSON. Specify
`--benchmark` when legacy traces omit dataset identity. `--train-fraction`
defaults to `0.8`; complete tasks and all their attempts stay together. See
[offline evaluation](docs/offline.md) for identity, units, split reuse and results.

## Documentation

The [documentation map](docs/README.md) links operational guides and technical
references for configuration, sidecar APIs, traces, call-load predictions,
lattice resources and PMU measurements. [Current validation](docs/CURRENT_PLAN.md)
records the testing boundary and remaining Linux acceptance work.

## Development

With development dependencies installed in the active Python environment:

```bash
python -m pip install -e 'services/sidecar[dev]'
python -m pytest tests -q
(cd services/sidecar && python -m pytest -q)
python tools/validate_contracts.py
(cd packages/clawtune-plugin && npm ci && npm test && npm run typecheck)
```

Run the two Python suites in their respective contexts. JSON Schemas in
`contracts/` are the public protocol source of truth. Do not commit secrets,
raw traces or runtime workspaces.
