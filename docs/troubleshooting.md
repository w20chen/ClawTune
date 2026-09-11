# Troubleshooting

Start with `python3 scripts/clawtune.py doctor`. Include its output, `uname -a`
and `git rev-parse --short HEAD` when reporting a failure. Never include secrets.
Rerun setup after a partial installation, update or checkout move; existing
configuration is preserved.

## Host setup and collector

| Symptom | Action |
| --- | --- |
| `apt-get` missing on openEuler | Use root setup; it selects apt/dnf. Do not run Debian-specific commands on openEuler. |
| BCC installed but Python cannot import it | Setup selects the system Python owning BCC and builds `.venv` with system packages. A Conda/pip-only BCC package is not a substitute. |
| `.venv/bin/python` missing or stale launcher path | Run `python3 scripts/clawtune.py setup` from the current checkout. |
| Missing kernel headers | Install the development package matching `uname -r`; check `/lib/modules/$(uname -r)/build`. `BCC_KERNEL_SOURCE` can select an existing matching tree. |
| BPF compile/attach or tracefs/cgroup permission error | Use `setup`/`check` through the privileged wrapper and inspect the real collector error. A trivial BPF program does not exercise ClawTune's collector. |
| `mvdan adapter is missing` | Rerun setup so the parser adapter is built for the actual sidecar user and architecture. Do not copy binaries between architectures. |
| amd64 image cannot start on Kunpeng | Follow [ARM/QEMU](arm-qemu.md); registration alone does not prove an image runs. |
| PMU counters unavailable | Inspect the artifact's unavailable reason. Permissions, unsupported events and multiplexing reduce coverage; they do not prove tool execution failed. |

`setup --skip-ebpf-check` is an installation opt-out, not accepted collector
validation. Correct the host and run `python3 scripts/clawtune.py check` before
accepting strict repository measurements.

## Sidecar or plugin startup

For `ECONNREFUSED 127.0.0.1:8765`, plugin readiness timeout, or unclear startup
stderr, run the sidecar explicitly to expose the error:

```bash
python3 scripts/clawtune.py sidecar
# another terminal
curl -fsS http://127.0.0.1:8765/health/ready
```

Health JSON must identify `service: clawtune-sidecar` and
`schema_version: clawtune.health.v1`; an unrelated listener is rejected.
Inspect port conflicts with `ss -ltnp`. In interactive use refresh sudo with
`sudo -v` if credentials expired; services need a site-approved privilege policy.
If `CLAWTUNE_TOKEN` is set, supply the same value to the plugin/launcher and use
an Authorization bearer header for authenticated API requests.

Setup enables the plugin and grants its conversation-hook permission. If
OpenClaw rejects `agent_end`, inspect:

```bash
openclaw config get plugins.entries.clawtune.hooks
openclaw config validate
```

`allowConversationAccess: true` is a sibling of `config`. Rerun setup and
restart the process that owns OpenClaw. A service restart command does not
restart an embedded `agent --local` process. If `plugins.allow` is configured,
include ClawTune and every other trusted provider/channel plugin you use; do
not replace an existing allow-list with an incomplete example.

## Provider and research search

An empty model trace usually means OpenClaw is not using the sidecar's `/v1`
proxy, the plugin is disabled, or provider configuration failed. Verify the
upstream URL, model name and credential in the relevant daily/benchmark config.
The local `CLAWTUNE_TOKEN` is not the upstream model key.

For a missing benchmark key, export `LLM_API_KEY` in the launch shell or put it
in `configs/llm_api_key.txt` selected by the common template. Use
`LLM_API_KEY_FILE` for a site-managed secret. Do not substitute `sudo -E`.

Research search needs Tavily credentials **and** a usable OpenClaw provider
installation. The task's `web-search-config.log` records provider linking or
fallback. Use the installed OpenClaw `plugins --help` to install the appropriate
provider package; do not assume a short plugin name is a package name. If
`enabled: false`, the runner disables search explicitly. BFCL web search is a
different backend and needs `SERPAPI_API_KEY`.

## Benchmark input or execution

- Missing tasks: use the exact `--dataset` path and inspect
  [default-source rules](benchmarks.md#data-and-paths). Verified has a historical
  `swebench_verified` directory; Terminal checkout directories vary by version.
- BFCL import errors: install its package/dependencies into `.venv`, and set
  `BFCL_REPO_PATH` to the Gorilla root or package directory. Memory/dependent
  entries and per-turn function additions are explicitly unsupported.
- Terminal format errors: use v1 `task.yaml`, not Harbor `task.toml`. Supply
  Compose or a Dockerfile. External host binds/build contexts are rejected;
  moving the task list does not move the referenced task directories.
- Docker errors: verify registry access, the actual image, Compose v2 when
  applicable, architecture and daemon settings. A dry-run does not test these.
- `agents.defaults.sandbox.docker.platform` rejected: remove that unsupported
  hand-written key. Use runner `docker.platform`/`SWE_REBENCH_DOCKER_PLATFORM`.
- No tool spans: the model may have answered without using a tool, or tool setup
  failed. Inspect agent logs and the bridge/provider configuration. A tool span
  alone still does not prove an eligible CPU/RSS or KB observation.

Use `run.json` and `report.json` under `.runtime/benchmarks/<benchmark>/<run>/`.
Task logs are under `traces/<task-digest>/`; shared sidecar logs are in `sidecar/`.
Repository diagnostics include `tool_resource_preflight_host.json`,
`sandbox-runtime-preflight.log` and `agent-stderr.txt`. Bridged multi-turn runs
preserve `turn-<index>-agent-stderr.txt` instead of one combined agent log.

A failed final drain leaves `kb_flush_complete: false`. An interrupted task may
have already learned; do not edit `active_tasks` or the durability flag to force
resume. Start a new run. Completed failed results are not retried by resume.

## Offline and seed errors

Use task traces as `offline --dataset`, not online task JSON or a whole run
including shared sidecar copies. Supply benchmark identity for older traces
and the correct `--rss-unit`. Singleton groups have no held-out tasks. See the
[offline guide](offline.md) for split reuse and coverage interpretation.

For a seed hash mismatch, compare with the checked-in immutable bundle. Seed
JSON is checked out as LF; newline conversions or manual edits invalidate its
byte hashes. Restore the intended bundle or create a new seed through the
export/storage tools; do not rewrite hashes to hide corruption. Keep all output
outside source datasets and immutable seed directories.
