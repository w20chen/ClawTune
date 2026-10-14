"""Small retrospective sensitivity study; frozen training, task-separated tuning.

Run: python research/execution_context_tuning_20261014/run.py
Reads existing evaluation artifacts only; never executes traced commands.
"""
import bisect
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/sidecar/src'))
from execution_context.duration import DurationContextKB

SOURCE = ROOT / 'research/lattice_worst30_20261014'
OUT = Path(__file__).resolve().parent
DEFAULT = (3, 2, 5, 3, .98)
NAMES = ('evidence_samples', 'evidence_tasks', 'failure_samples', 'failure_tasks', 'failure_fraction')
EDGES = (100, 500, 2000, 10000)


def read(name):
    p = SOURCE / name
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix == '.gz' else p.read_bytes())


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')


def summary(group):
    if not group:
        return None
    values = sorted(s['duration_ms'] for s in group)
    counts = Counter(bisect.bisect_right(EDGES, v) for v in values)
    return dict(n=len(values), tasks=len({s['task'] for s in group}), p50=statistics.median(values),
                p90=values[math.ceil(.9 * len(values)) - 1],
                bucket=max(range(5), key=lambda i: (counts[i], -i)))


def prepare(kb, rows, keys):
    cache, prepared = {}, []
    for row in rows:
        pair = keys.get(row['id'], {}).get('keys', {})
        sig = (pair.get('evidence'), pair.get('failure'))
        if sig not in cache:
            e = summary(kb.groups['evidence'].get(sig[0], []))
            group = kb.groups['failure'].get(sig[1], [])
            f = summary(group)
            counts = Counter(s['failure'] for s in group if s['failure'])
            fail = None
            if counts:
                label, n = counts.most_common(1)[0]
                fail = summary([s for s in group if s['failure'] == label])
                fail.update(fraction=n / len(group), label=label)
            cache[sig] = e, f, fail
        prepared.append((row, *cache[sig]))
    return prepared


def predict(item, policy=DEFAULT, mode='both'):
    row, e, f, fail = item
    base = row['p50_ms']
    probs = row['prediction']['buckets']['probabilities']
    bucket = max(range(len(probs)), key=lambda i: (probs[i], -i)) if probs else None
    result = dict(p50=base, p90=row['p90_ms'], bucket=bucket, reason=None)
    if base is None:
        return result
    if mode in ('both', 'evidence') and e and e['n'] >= policy[0] and e['tasks'] >= policy[1]:
        result.update(p50=e['p50'], p90=e['p90'], bucket=e['bucket'], reason='evidence')
    if (mode in ('both', 'failure') and fail and f['n'] >= policy[2] and f['tasks'] >= policy[3]
            and fail['fraction'] >= policy[4] and fail['p50'] < result['p50']):
        result.update(p50=fail['p50'], p90=fail['p90'], bucket=fail['bucket'], reason='failure')
    return result


def metrics(items, predictions):
    pairs = [(i[0], p) for i, p in zip(items, predictions) if i[0]['actual_ms'] is not None and p['p50'] is not None]
    n = len(pairs)
    return dict(calls=len(items), scored=n, corrected=sum(p['reason'] is not None for p in predictions),
        scored_corrected=sum(p['reason'] is not None for r, p in pairs),
        reasons=dict(Counter(p['reason'] for r, p in pairs if p['reason'])),
        mae_s=statistics.mean(abs(p['p50'] - r['actual_ms']) / 1000 for r, p in pairs),
        bucket_accuracy=sum(p['bucket'] == bisect.bisect_right(EDGES, r['actual_ms']) for r, p in pairs)/n,
        factor2=sum(.5*r['actual_ms'] <= p['p50'] <= 2*r['actual_ms'] for r, p in pairs)/n,
        over10=sum(p['p50'] > 10*r['actual_ms'] for r, p in pairs),
        under10=sum(r['actual_ms'] > 10*p['p50'] for r, p in pairs),
        p90_coverage=sum(r['actual_ms'] <= p['p90'] for r, p in pairs if p['p90'] is not None)/sum(p['p90'] is not None for r, p in pairs),
        improved=sum(abs(p['p50']-r['actual_ms']) < r['absolute_error_ms'] for r, p in pairs),
        worsened=sum(abs(p['p50']-r['actual_ms']) > r['absolute_error_ms'] for r, p in pairs))


def paired(items, before, after):
    groups = defaultdict(list)
    for item, a, b in zip(items, before, after):
        r = item[0]
        if r['actual_ms'] is not None and a['p50'] is not None:
            groups[r['task']].append((abs(b['p50']-r['actual_ms'])-abs(a['p50']-r['actual_ms']))/1000)
    clusters = [(sum(v), len(v)) for v in groups.values()]
    rng = random.Random(20261014)
    draws = []
    for _ in range(2000):
        selected = rng.choices(clusters, k=len(clusters))
        draws.append(sum(x[0] for x in selected)/sum(x[1] for x in selected))
    return dict(delta_mae_s=sum(x[0] for x in clusters)/sum(x[1] for x in clusters),
                ci95=[sorted(draws)[49], sorted(draws)[1949]], tasks=len(clusters), draws=2000)


def main():
    rows = read('context-predictions.json.gz')
    assert all(r['tool'] in ('exec', 'terminal_exec') for r in rows)
    manifest, keys, model = read('split.json'), read('context-keys.json.gz'), read('execution-context-kb.json.gz')
    kb = DurationContextKB()
    for s in model['samples']:
        assert s['task'] in manifest['train']
        assert kb.observe(s['keys'], ident=s['id'], task=s['task'], duration_ms=s['duration_ms'], failure=s['failure'])
    items = prepare(kb, rows, keys)
    # Verify the accelerated sweep against production, including untouched calls.
    for item in items:
        r = item[0]
        actual = kb.predict(keys.get(r['id'], {}).get('keys', {}), r['p50_ms'])
        p = predict(item)
        assert p['p50'] == (actual['p50'] if actual else r['p50_ms']) == r['corrected_p50_ms']
        assert p['p90'] == (actual['p90'] if actual else r['p90_ms'])
        assert bool(p['reason']) == bool(actual)
        if actual:
            buckets = Counter(bisect.bisect_right(EDGES, v) for v in actual['values'])
            assert p['bucket'] == max(range(5), key=lambda i: (buckets[i], -i))
    tasks = sorted({r['task'] for r in rows}, key=lambda t: hashlib.sha256(('tune-20261014:' + t).encode()).hexdigest())
    dev_tasks, check_tasks = tasks[::2], tasks[1::2]
    assert not set(dev_tasks) & set(check_tasks)
    assert not set(tasks) & set(manifest['train'])
    dev = [i for i in items if i[0]['task'] in dev_tasks]
    check = [i for i in items if i[0]['task'] in check_tasks]
    # One-coordinate local search limits the number of choices and overfitting.
    candidates = [DEFAULT]
    for index, values in enumerate(((2, 5, 10), (1, 3, 5), (3, 10, 20), (2, 5), (.9, .95, 1.0))):
        for value in values:
            p = list(DEFAULT)
            p[index] = value
            candidates.append(tuple(p))
    protocol = dict(default=dict(zip(NAMES, DEFAULT)), candidates=[dict(zip(NAMES, p)) for p in candidates],
        objective='Minimum tuning MAE; ties prefer defaults, then fewer changes. Check split is not used for selection.',
        dev_tasks=dev_tasks, check_tasks=check_tasks, train_tasks=manifest['train'], ttl_seconds=600,
        scope='Retrospective existing-trace study, not a fresh unseen benchmark; existing holdout split into tuning and check tasks.',
        files_sha256={n: hashlib.sha256((SOURCE/n).read_bytes()).hexdigest() for n in ('context-predictions.json.gz', 'context-keys.json.gz', 'execution-context-kb.json.gz', 'split.json')})
    save('protocol.json', protocol)
    sweep = [dict(policy=dict(zip(NAMES, p)), metrics=metrics(dev, [predict(i, p) for i in dev])) for p in candidates]
    chosen_index = min(range(len(candidates)), key=lambda j: (sweep[j]['metrics']['mae_s'], j))
    chosen = candidates[chosen_index]
    save('selection.json', dict(selected=sweep[chosen_index], sweep=sweep))
    report = dict(selected=dict(zip(NAMES, chosen)), default_verified_calls=len(items), model_samples=len(kb.samples),
                  test_updates=0, target_commands_executed=0, source_access='read-only', results={})
    for name, part in (('tuning', dev), ('check', check), ('all_retrospective', items)):
        outputs = {mode: [predict(i, mode=mode) for i in part] for mode in ('off', 'evidence', 'failure', 'both')}
        outputs['tuned'] = [predict(i, chosen) for i in part]
        report['results'][name] = dict(metrics={m: metrics(part, ps) for m, ps in outputs.items()},
            default_vs_off=paired(part, outputs['off'], outputs['both']),
            tuned_vs_default=paired(part, outputs['both'], outputs['tuned']))
    audit = []
    for item in items:
        r = item[0]
        audit.append(dict(id=r['id'], task=r['task'], actual_ms=r['actual_ms'],
                          split='tuning' if r['task'] in dev_tasks else 'check',
                          baseline=predict(item, mode='off'), default=predict(item), tuned=predict(item, chosen)))
    (OUT / 'predictions.json.gz').write_bytes(gzip.compress(json.dumps(audit, allow_nan=False).encode(), mtime=0))
    report['production_defaults_changed'] = False
    report['decision'] = ('Retain defaults: the selected relaxation has small gains, its task-bootstrap '
                          'check interval includes zero, and p90 coverage decreases. This is an '
                          'interpretation of the results, not a preregistered deployment rule.')
    report['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    assert len(kb.samples) == len(model['samples'])
    save('results.json', report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
