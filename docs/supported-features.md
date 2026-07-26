# Supported User Workflows

## Supported

- Run the sidecar locally on `127.0.0.1:8765`.
- Install and enable the `agent-scheduler` OpenClaw plugin.
- Route OpenClaw model traffic through the sidecar OpenAI-compatible proxy.
- Record schema v6 JSONL traces under `data/traces`.
- Record hook-visible tool args/results with `recordRawTrace: true`.
- Attribute `exec` resource usage with `executionBackend: "managed-wrapper"`.
- Run SWE-Rebench batches with `--sample`, `--skip`, `--instance-ids`,
  `--repo`, `--parallelism`, and `--export`.

## Not The Goal Yet

- CPU-side optimization policy.
- GPU/KV-cache scheduling.
- Exact per-process network accounting.
- OpenClaw core modifications.

## Validate

```bash
python tools/validate_contracts.py
python -m pytest tests -q --basetemp .pytest-tmp-root

cd services/scheduler
python -m pytest tests -q

cd ../../packages/openclaw-plugin
npm test
npm run typecheck
```
