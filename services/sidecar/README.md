# ClawTune sidecar

The sidecar owns model proxying, lifecycle APIs, tracing, resource collection
and prediction KBs. For production setup, use the repository's
[installation guide](../../docs/getting-started.md).

For development, from this directory:

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

[Sidecar reference](../../docs/sidecar.md) covers APIs, startup and collection.
[Configuration](../../docs/configuration.md) covers environment and credentials.
A plain unprivileged launch does not validate strict eBPF measurement; use the
root `scripts/clawtune.py setup`, `check` and `sidecar` commands on Linux.
