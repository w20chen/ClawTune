"""Current-code weight ablation; read-only measured trace exports + synthetic controls.

Run from repository root: python research/edge_weight_audit_20261014/run.py
No traced command is executed. Production code and saved KBs are never modified.
"""
import bisect
from collections import Counter, defaultdict
from dataclasses import asdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'services/sidecar/src'), str(ROOT)]
import numpy as np
from edge_kappa_kb import EdgeKappaKB, FeatureQuery, TimeOutcome, TrainingEvent
from tool_time.edge_kappa_adapter import shell_query

OUT = Path(__file__).resolve().parent
SOURCE = ROOT / 'research/kb_systematic_20261013/results/events.json.gz'
EDGES = (100, 500, 2000, 10000)
METRICS = ('accuracy', 'log_loss', 'brier', 'mae_s', 'log_mae')


def save(name, obj):
    data = (json.dumps(obj, ensure_ascii=False, indent=None, allow_nan=False) + '\n').encode()
    (OUT / name).write_bytes(gzip.compress(data, mtime=0) if name.endswith('.gz') else data)


def log(**kw):
    print(json.dumps(kw), flush=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def score(pred, actual, p50):
    if pred.probabilities is None:
        return None
    b = bisect.bisect_right(EDGES, actual)
    return [float(pred.bucket == b), -math.log(pred.probabilities[b]),
            sum((v - (i == b)) ** 2 for i, v in enumerate(pred.probabilities)),
            abs(p50-actual)/1000, abs(math.log1p(p50/1000)-math.log1p(actual/1000))]


def evaluate(kb, cases, seed):
    original = {k: e.log_kappa for k, e in kb.edges.items()}
    initial = {k: math.log(kb.k0 / len(kb.graph.parents[k[1]])) for k in original}
    shuffled = dict(original)
    rng = random.Random(seed)
    # Shuffle only within a child: preserve each child's total sharing strength.
    for child, parents in kb.graph.parents.items():
        vals = [original[p, child] for p in parents]
        rng.shuffle(vals)
        shuffled.update({(p, child): v for p, v in zip(parents, vals)})
    rows = []
    for name, weights in [('learned', original), ('fixed', initial), ('shuffled', shuffled)]:
        for k, v in weights.items():
            kb.edges[k].log_kappa = v
        cache = {}
        for meta, event in cases:
            if event.query not in cache:
                distribution = kb.predict_duration(event.query)
                p50, cumulative = None, 0
                threshold = sum(distribution.weights) * .5
                for value, weight in sorted(zip(distribution.values_ms, distribution.weights)):
                    cumulative += weight
                    if cumulative >= threshold:
                        p50 = value
                        break
                cache[event.query] = distribution.bucket_prediction, p50
            p, p50 = cache[event.query]
            rows.append(dict(model=name, id=event.outcome.event_id, task=event.outcome.task_id,
                repo=meta['repo_group'], partition=meta['partition'], actual_ms=event.outcome.duration_ms,
                score=score(p, event.outcome.duration_ms, p50), p50_ms=p50, bucket=p.bucket, probabilities=p.probabilities,
                exact=p.exact_match, own_n=p.observation_count,
                parent_share=sum(e.mixture_share for e in p.evidence)))
    for k, v in original.items():
        kb.edges[k].log_kappa = v
    return rows


def summarize(rows):
    result = {}
    for part in sorted({r['partition'] for r in rows}):
        by = {m: {r['id']: r for r in rows if r['partition'] == part and r['model'] == m}
              for m in ('learned', 'fixed', 'shuffled')}
        rr = list(by['learned'].values())
        out = dict(n=len(rr), tasks=len({r['task'] for r in rr}), models={})
        for m, values in by.items():
            scored = [r['score'] for r in values.values() if r['score'] is not None]
            out['models'][m] = dict(predicted=len(scored), coverage=len(scored)/len(rr),
                **dict(zip(METRICS, np.mean(scored, axis=0).tolist())))
        out['paired'] = {}
        for m in ('fixed', 'shuffled'):
            groups = defaultdict(lambda: np.zeros(1+len(METRICS)))
            changed = Counter()
            for ident, a in by['learned'].items():
                b = by[m][ident]
                assert (a['score'] is None) == (b['score'] is None)
                assert a['exact'] == b['exact'] and a['own_n'] == b['own_n']
                if a['score'] is None:
                    continue
                groups[a['task']] += np.r_[1, np.array(a['score']) - b['score']]
                changed['scored'] += 1
                changed['probabilities_changed'] += int(a['probabilities'] != b['probabilities'])
                changed['bucket_changed'] += int(a['bucket'] != b['bucket'])
                changed['p50_changed'] += int(a['p50_ms'] != b['p50_ms'])
                changed['exact'] += int(a['exact'])
            data = np.array(list(groups.values()))
            total = data.sum(axis=0)
            rng = np.random.default_rng(20261014)
            boot = np.array([data[rng.integers(0, len(data), len(data))].sum(axis=0) for _ in range(2000)])
            out['paired'][m] = dict(changes=dict(changed), learned_minus_control={
                metric: dict(delta=total[i]/total[0], ci95=np.quantile(boot[:, i]/boot[:, 0], [.025, .975]).tolist())
                for i, metric in enumerate(METRICS, 1)})
        result[part] = out
    return result


def synthetic():
    runs = []
    for seed in (17, 42, 101, 2026, 9001):
        design = [(a, b) for _ in range(100) for a in range(2) for b in range(2)]
        random.Random(seed).shuffle(design)
        weights = {}
        for label, high in [('none', 20), ('within_bucket', 80), ('cross_bucket', 200), ('larger_same_bucket', 400)]:
            events = []
            for i, (a, b) in enumerate(design):
                q = FeatureQuery(frozenset({'tool=controlled', f'work={a}', f'irrelevant={b}'}),
                                 frozenset({'tool=controlled'}), 'controlled')
                y = high if a else 20
                events.append(TrainingEvent(q, TimeOutcome(str(i), i*2., i*2.+1, 'synthetic', str(i), '0', duration_ms=y)))
            kb = EdgeKappaKB.fit(events)
            weights[label] = {k: e.log_kappa for k, e in kb.edges.items()}
            effects = defaultdict(list)
            for (p, c), edge in kb.edges.items():
                dropped = kb.graph.signatures[c] - kb.graph.signatures[p]
                if len(kb.graph.signatures[c]) == 3:
                    effects[next(iter(dropped)).split('=')[0]].append(math.exp(edge.log_kappa))
            cases = [(dict(repo_group='synthetic', partition=label), e) for e in events[:4]]
            rows = evaluate(kb, cases, seed)
            # Same feature counts and labels give exact fixed-weight training equivalence.
            fixed = EdgeKappaKB.fit(events, {'learn_weights': False})
            for e in events[:4]:
                expected = next(r for r in rows if r['id'] == e.outcome.event_id and r['model'] == 'fixed')
                assert tuple(expected['probabilities']) == fixed.predict(e.query).probabilities
            runs.append(dict(seed=seed, world=label, low_ms=20, high_ms=high,
                true_delta_ms=high-20, mean_kappa_by_dropped_parameter={k: statistics.mean(v) for k,v in effects.items()},
                gradient_updates=sum(e.update_count for e in kb.edges.values())))
        runs.append(dict(seed=seed, identical_weights_none_vs_within=weights['none'] == weights['within_bucket'],
                         identical_weights_200_vs_400=weights['cross_bucket'] == weights['larger_same_bucket']))
    save('synthetic.json', runs)
    log(stage='synthetic_done')


def main():
    start = time.time()
    raw = json.loads(gzip.decompress(SOURCE.read_bytes()))
    # Preserve the existing trace admission and train partition; reparse all commands.
    cases = []
    for r in raw:
        e = r['event']
        repo = 'swe-rebench:' + r['repo_group']
        query = shell_query(e['source_command'], repo=repo)
        cases.append((r, TrainingEvent(query, TimeOutcome(**e['outcome']), e['source_command'])))
    train = [e for r,e in cases if r['partition'] == 'train']
    test = [(r,e) for r,e in cases if r['partition'] in ('dev', 'test')]
    assert {e.outcome.task_id for e in train}.isdisjoint({e.outcome.task_id for _,e in test})
    source_files = [p for folder in ('edge_kappa_kb', 'command_parser', 'kb_common')
                    for p in (ROOT/'services/sidecar/src'/folder).rglob('*') if p.suffix in ('.py', '.json')]
    hashes = {str(p.relative_to(ROOT)): digest(p) for p in source_files}
    save('protocol.json', dict(source=str(SOURCE.relative_to(ROOT)), source_sha256=digest(SOURCE),
        command='python research/edge_weight_audit_20261014/run.py', source_files=hashes,
        counts=dict(Counter(r['partition'] for r,_ in cases)),
        train_tasks=len({e.outcome.task_id for e in train}),
        target='exact measured shell clause duration buckets, not CPU/RSS or whole-call duration',
        provenance='existing admitted v5 exec/terminal_exec measured event export; labels and timestamps unchanged; current parser',
        split='existing SQLGlot train/dev tasks and PennyLane test; retrospective, no test tuning',
        variants='same graph/counts/events: learned, initial fixed k0/parent_count, within-child shuffled learned weights',
        seeds=[17,42,9001], bucket_edges_ms=EDGES, max_optional_features=6,
        uncertainty='2000 paired task bootstrap samples, conditional on these repositories',
        synthetic='balanced randomized 2x2 design; work parameter affects duration, irrelevant parameter has zero effect'))
    synthetic()
    results = {}
    for seed in (17,42,9001):
        log(stage='fit_start', seed=seed, events=len(train))
        kb = EdgeKappaKB.fit(train, {'replay_seed': seed})
        kb.freeze()
        log(stage='fit_done', seed=seed, seconds=time.time()-start, nodes=len(kb.nodes), edges=len(kb.edges))
        rows = evaluate(kb, test, seed)
        save(f'predictions-{seed}.json.gz', rows)
        results[str(seed)] = dict(metrics=summarize(rows), nodes=len(kb.nodes), edges=len(kb.edges),
            updated_edges=sum(e.update_count > 0 for e in kb.edges.values()),
            gradient_updates=sum(e.update_count for e in kb.edges.values()))
        save('results.json', results)
        log(stage='scored', seed=seed, metrics=results[str(seed)]['metrics'])
        del kb
    assert all(digest(ROOT/p) == h for p,h in hashes.items()), 'source changed while running'
    save('verification.json', dict(source_unchanged=True, task_disjoint=True, fixed_training_equivalence=True,
                                  seconds=time.time()-start, completed_seeds=list(results)))


if __name__ == '__main__':
    main()
