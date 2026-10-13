"""Metrics and artifact I/O for the execution-context regression."""
import gzip
import json
import math
from pathlib import Path
import random
import statistics
from collections import defaultdict


def read(path):
    path = Path(path)
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode()
    path.write_bytes(gzip.compress(data, mtime=0) if path.suffix == '.gz' else data)


def metrics(rows):
    valid = [r for r in rows if r['p50'] is not None]
    def mean(values):
        return statistics.mean(values) if values else None
    return {'n': len(rows), 'predicted': len(valid), 'coverage': len(valid)/len(rows) if rows else None,
        'patched': sum(r.get('patched', False) for r in valid),
        'accuracy': mean([r['predicted_bucket'] == r['actual_bucket'] for r in valid]),
        'mae_s': mean([abs(r['p50']-r['actual_ms'])/1000 for r in valid]),
        'log_mae': mean([abs(math.log1p(r['p50']/1000)-math.log1p(r['actual_ms']/1000)) for r in valid]),
        'factor2': mean([.5*r['actual_ms'] <= r['p50'] <= 2*r['actual_ms'] for r in valid]),
        'over10_n': sum(r['p50'] > 10*r['actual_ms'] for r in valid),
        'under10_n': sum(r['actual_ms'] > 10*r['p50'] for r in valid),
        'over100_n': sum(r['p50'] > 100*r['actual_ms'] for r in valid),
        'under100_n': sum(r['actual_ms'] > 100*r['p50'] for r in valid)}


def paired(base, patched):
    grouped = defaultdict(list)
    for a,b in zip(base,patched):
        assert a['id'] == b['id'] and (a['p50'] is None) == (b['p50'] is None)
        if a['p50'] is not None:
            y = math.log1p(a['actual_ms']/1000)
            grouped[a['task']].append((abs(math.log1p(b['p50']/1000)-y)-abs(math.log1p(a['p50']/1000)-y),
                int(b['predicted_bucket']==b['actual_bucket'])-int(a['predicted_bucket']==a['actual_bucket'])))
    clusters = [(sum(x[0] for x in v),sum(x[1] for x in v),len(v)) for v in grouped.values()]
    rng=random.Random(20261013)
    draws=[[],[]]
    for _ in range(2000):
        sample = rng.choices(clusters,k=len(clusters))
        n=sum(c[2] for c in sample)
        for i in range(2):
            draws[i].append(sum(c[i] for c in sample)/n)
    return {'unit': 'task-cluster bootstrap, 2000 draws', 'tasks':len(clusters),
        **{name: {'delta':sum(c[i] for c in clusters)/sum(c[2] for c in clusters),
                  'ci95':[sorted(draws[i])[49],sorted(draws[i])[1949]]}
           for i,name in enumerate(('log_mae','accuracy'))}}
