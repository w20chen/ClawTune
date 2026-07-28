# Batch Benchmark Usage

Use the local SWE-Rebench runner for this project:

```bash
cp swe_rebench/config.example.yaml swe_rebench/config.yaml
python -m swe_rebench.runner prepare --config swe_rebench/config.yaml
python -m swe_rebench.discover --sample 20 --out swe_rebench/tasks.json
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json --sample 10 --export
```

See `swe_rebench/README.md` for the full `swe_rebench.runner` CLI reference,
including task source, task selection, runtime override, export, and config-only
options.

Select exact tasks:

```bash
python -m swe_rebench.runner run --config swe_rebench/config.yaml \
  --dataset swe_rebench/tasks.json \
  --instance-ids django__django-12345,sympy__sympy-67890
```

This repository treats `C:\Users\user\Desktop\agent-test-bench` as a read-only
reference source.
