# agent-test-bench Reference

This repository does not modify or import agent-test-bench at runtime.

For SWE-Rebench task discovery, `swe_rebench.discover` can read:

```text
AGENT_TEST_BENCH_ROOT/data/swe-rebench/tasks.json
../agent-test-bench/data/swe-rebench/tasks.json
```

Typical flow:

```bash
python -m swe_rebench.discover --sample 20 --out swe_rebench/tasks.json
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json --sample 10 --export
```

Use `tools/import_agent_test_bench_trace.py` only for offline trace conversion.
