"""Evaluate the single execution-context implementation without running tools.

python -m offline.evaluate_execution_context
Use --refresh-context --dataset PATH to rebuild context from read-only traces.
"""
import argparse
import bisect
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/sidecar/src'))

from execution_context.duration import DurationContextKB, context_keys
from execution_context.history import (command_input, contexts, exit_label, failure_label,
                                       failure_snapshot, params, replay)
from offline.execution_context_metrics import metrics, paired, read, save

EDGES = (100, 500, 2000, 10000)


def extract(source, dataset, output):
    started = time.perf_counter()
    inventory = read(source / 'inventory.json')
    index = {(c['trace']['file'], c['trace']['action_id']): c for c in read(source / 'calls.json.gz')}
    files = {f['path']: f['sha256'] for t in inventory['tasks'].values() for f in t['files']}
    dataset = dataset or Path(inventory['dataset'])
    records, prefix_checks = {}, 0
    for name, expected in sorted(files.items()):
        blob = (dataset / name).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == expected, name
        events = []
        for line_no, line in enumerate(blob.decode('utf-8-sig').splitlines(), 1):
            raw = json.loads(line)
            if raw.get('type') != 'action' or raw.get('action_type') != 'tool_exec':
                continue
            data = raw.get('data') or {}
            events.append({'id': raw['action_id'], 'line': line_no, 'start': raw.get('ts_start'),
                           'end': raw.get('ts_end'), 'tool': data.get('tool_name'),
                           'args': params(data.get('tool_args')), 'output': data.get('tool_result')})
        evidence = replay(events, name)
        for e in events:
            call = index.get((name, e['id']))
            if call is None or call['tool'] not in ('exec', 'terminal_exec'):
                continue
            assert call['command'] == command_input(e['args'])
            state = {'parsed': contexts(e['args']), 'snapshots': evidence.get(e['id'], []),
                     'failure': failure_snapshot(e, events)}
            keys = context_keys(call['repo'], state, call['clauses'] if not call.get('parse_reason') else [])
            code = exit_label(e['output'])
            label = failure_label(e['output']) if call['clauses'] and code not in (None, 0) else None
            records[call['id']] = {'keys': keys, 'failure': label}
        # A real prefix per trace, including deletion of all future events and
        # replacement of the current result. No current output may affect input.
        if events:
            chosen = events[len(events) // 2]
            prefix = copy.deepcopy([e for e in events if e['line'] <= chosen['line']])
            prefix[-1]['output'] = 'COUNTERFACTUAL CURRENT OUTPUT\nExit code: 0'
            assert replay(prefix, name).get(chosen['id']) == evidence.get(chosen['id'])
            assert failure_snapshot(chosen, prefix) == failure_snapshot(chosen, events)
            prefix_checks += 1
    save(output / 'context.json.gz', records)
    save(output / 'extraction.json', {'source_files_verified': len(files), 'prefix_checks': prefix_checks,
         'calls_sha256': hashlib.sha256((source / 'calls.json.gz').read_bytes()).hexdigest(),
         'history_sha256': hashlib.sha256((ROOT / 'services/sidecar/src/execution_context/history.py').read_text(encoding='utf8').encode('utf8')).hexdigest(),
         'elapsed_s': time.perf_counter() - started})


def evaluate(rows, records, kb):
    result = []
    for row in rows:
        record = records.get(row['id'], {})
        correction = kb.predict(record.get('keys', {}), row['p50'])
        out = {**row, 'patched': bool(correction)}
        if correction:
            counts = [0] * (len(EDGES) + 1)
            for value in correction['values']:
                counts[bisect.bisect_right(EDGES, value)] += 1
            out.update(p50=correction['p50'], p90=correction['p90'],
                       predicted_bucket=max(range(len(counts)), key=lambda i: (counts[i], -i)),
                       correction=correction)
        result.append(out)
    return result


def transition(before, after, factor):
    result = {'fixed': [], 'new': []}
    for a, b in zip(before, after):
        if a['p50'] is None:
            continue
        bad_a = a['p50'] > factor * a['actual_ms'] or a['actual_ms'] > factor * a['p50']
        bad_b = b['p50'] > factor * b['actual_ms'] or b['actual_ms'] > factor * b['p50']
        if bad_a and not bad_b:
            result['fixed'].append(a['id'])
        elif bad_b and not bad_a:
            result['new'].append(a['id'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'research/execution_context')
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--refresh-context', action='store_true')
    parser.add_argument('--extract-only', action='store_true')
    args = parser.parse_args()
    source = args.study / 'research/kb_systematic_20261013/results'
    args.output.mkdir(parents=True, exist_ok=True)
    if args.refresh_context or not (args.output / 'context.json.gz').exists():
        extract(source, args.dataset, args.output)
    if args.extract_only:
        return
    split = read(args.output / 'split.json')
    records = read(args.output / 'context.json.gz')
    assert read(args.output / 'extraction.json')['calls_sha256'] == hashlib.sha256((source / 'calls.json.gz').read_bytes()).hexdigest()
    assert read(args.output / 'extraction.json')['history_sha256'] == hashlib.sha256(
        (ROOT / 'services/sidecar/src/execution_context/history.py').read_text(encoding='utf8').encode('utf8')).hexdigest(), 'rebuild trace context with --refresh-context'
    with gzip.open(source / 'predictions.jsonl.gz', 'rt', encoding='utf8') as stream:
        rows = [json.loads(line) for line in stream if 'lattice_shrinkage' in line]
    rows = [r for r in rows if r['model'] == 'lattice_shrinkage' and r['tool'] in ('exec', 'terminal_exec')]
    parts = {p: [r for r in rows if r['task'] in split[p + '_tasks']] for p in ('train', 'dev', 'test')}
    kb = DurationContextKB()
    for r in parts['train']:
        record = records.get(r['id'], {})
        kb.observe(record.get('keys', {}), ident=r['id'], task=r['task'],
                   duration_ms=r['actual_ms'], failure=record.get('failure'))
    model_path = args.output / 'execution-context-kb.json'
    save(model_path, kb.to_json_obj())
    kb = DurationContextKB.from_json_obj(read(model_path))
    result = evaluate(parts['test'], records, kb)
    # Regression oracle is a hash, never a dependency on retired implementations.
    prediction_hash = hashlib.sha256(json.dumps([(r['id'], r['p50'], r['predicted_bucket']) for r in result]).encode()).hexdigest()
    expected = args.output / 'expected.json'
    if expected.exists():
        assert read(expected)['prediction_sha256'] == prediction_hash, 'consolidated prediction regression'
    train_ids = {r['id'] for r in parts['train']}
    assert all(set(r.get('correction', {}).get('evidence_ids', [])) <= train_ids for r in result)
    poisoned = evaluate([{**r, 'actual_ms': 99999999, 'actual_bucket': 4} for r in parts['test']], records, kb)
    assert [(r['p50'], r['predicted_bucket']) for r in result] == [(r['p50'], r['predicted_bucket']) for r in poisoned]
    save(args.output / 'predictions.json.gz', {'baseline': parts['test'], 'final': result})
    report = {'counts': {k: len(v) for k, v in parts.items()}, 'baseline': metrics(parts['test']),
              'final': metrics(result), 'model_samples': len(kb.samples), 'prediction_sha256': prediction_hash,
              'model_sha256': hashlib.sha256(model_path.read_bytes()).hexdigest(),
              'transitions': {str(f): transition(parts['test'], result, f) for f in (10, 100)},
              'paired': paired(parts['test'], result), 'test_labels_excluded_from_prediction': True,
              'source_access': 'read-only', 'target_commands_executed': 0,
              'scope': 'previously inspected task holdout; retrospective regression'}
    save(args.output / 'results.json', report)
    print(json.dumps({k: report[k] for k in ('counts', 'baseline', 'final', 'model_samples', 'prediction_sha256')}, indent=2))


if __name__ == '__main__':
    main()
