"""Repeat the previous corpus/protocol and measure detached Edge captures.

Uses only copies of training data. The baseline results and source bundle are
retained in kb_online_cost_20261012; this probe does not overwrite either.
"""
import json
import sys
import time
from pathlib import Path

from research.kb_online_cost_20261012 import study

captures = []
latest_payload = None
after_updates = []
original_timed = study.timed


def timed(fn, *args, **kwargs):
    value, elapsed = original_timed(fn, *args, **kwargs)
    if getattr(fn, '__name__', '') == '_advance' and args == (4e12,):
        root = Path(sys.argv[sys.argv.index('--root') + 1])
        samples = json.loads((root / 'inputs.json').read_bytes())['samples']
        times = []
        for i, sample in enumerate(samples):
            if sample['reason']:
                continue
            evidence, query_ms = original_timed(fn.__self__.predict_load_samples,
                sample['repo'], sample['clauses'], 4e12 + i)
            _, compose_ms = original_timed(study.compose, 'lattice', evidence,
                study.load_bucket_edges((100, 500, 2000, 10000)), clauses=sample['clauses'])
            times.append(query_ms + compose_ms)
        after_updates.append(study.stats(times))
    return value, elapsed


def detached_snapshot(runtime):
    global latest_payload
    started = time.perf_counter()
    materialize = runtime.capture_snapshot()
    captured = time.perf_counter()
    result = materialize()
    latest_payload = result
    finished = time.perf_counter()
    captures.append({'capture_ms': (captured - started) * 1000,
                     'materialize_ms': (finished - captured) * 1000})
    return result


if __name__ == '__main__':
    study.EdgeKappaRuntime.to_snapshot = detached_snapshot
    study.timed = timed
    study.main()
    out = Path(sys.argv[sys.argv.index('--out') + 1])
    name = f'{sys.argv[1]}-{sys.argv[2]}'
    if '--training-count' in sys.argv:
        name += '-n' + sys.argv[sys.argv.index('--training-count') + 1]
    path = out / (name + '.json')
    result = json.loads(path.read_bytes())
    result['detached_snapshots'] = captures
    result['first_queries_after_each_update_batch'] = after_updates
    if latest_payload is not None:
        from clawtune_sidecar.predictors.tool_resource import _write_json_atomic
        stream_times = []
        for _ in range(2):
            _, ms = original_timed(_write_json_atomic, out / (name + '-stream.json'), latest_payload)
            stream_times.append(ms)
        result['stream_snapshot_cold_then_reused_ms'] = stream_times
        result['stream_snapshot_bytes'] = (out / (name + '-stream.json')).stat().st_size
    path.write_text(json.dumps(result, indent=2))
