"""Compare the shared correction on the same freshly evaluated task split."""
import gzip
import json
import math
import statistics
import time
from collections import defaultdict

from run import (ROOT, OUT, DATASET, Capture, Empty, finite_duration, save,
                 raw_actions, runner, predict_call_load, ToolCallQuery, load_bucket_edges)
from execution_context.history import contexts, params, replay, failure_snapshot, exit_label, failure_label
from execution_context.duration import context_keys, DurationContextKB

def read(name):
    p = OUT / name
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix == '.gz' else p.read_bytes())

def main():
    started = time.monotonic()
    manifest = read('split.json')
    kb = DurationContextKB()
    keys_by_call, query_cache = {}, {}
    edges = load_bucket_edges((100, 500, 2000, 10000))
    for i, key in enumerate(manifest['train'] + manifest['test']):
        task = manifest['tasks'][key]
        raw = list(raw_actions(DATASET, task))
        by_file = defaultdict(list)
        for f, n, row in raw:
            data = row['data']
            by_file[f].append({'id': row['action_id'], 'line': n, 'start': row.get('ts_start'),
                'end': row.get('ts_end'), 'tool': data.get('tool_name'),
                'args': params(data.get('tool_args')), 'output': data.get('tool_result')})
        snapshots = {f: replay(events, f) for f, events in by_file.items()}
        event_index = {(f, e['id']): e for f, events in by_file.items() for e in events}
        loaded = runner.load_task(DATASET, task, 'MiB')
        eligible = [(f, n, r) for f, n, r in raw if finite_duration(r)]
        assert len(eligible) == len(loaded.calls)
        for index, call in enumerate(loaded.calls):
            if call.censored or call.tool_name not in ('exec', 'terminal_exec'):
                continue
            f, n, raw_row = eligible[index]
            event = event_index[(f, raw_row['action_id'])]
            known = runner._safe_recorded_clause(call.command, loaded.call_clauses[index])
            cache_key = json.dumps([call.repo, call.tool_name, call.command, known], sort_keys=True)
            if cache_key not in query_cache:
                cap = Capture(Empty())
                predict_call_load(runtime=Empty(), trie=Empty(), lattice=cap,
                    query=ToolCallQuery(call.repo, call.tool_name, call.command, 1e12), edges=edges, parsed_clauses=known)
                query_cache[cache_key] = cap.clauses
            clauses = query_cache[cache_key]
            state = {'parsed': contexts(event['args']), 'snapshots': snapshots[f].get(event['id'], []),
                     'failure': failure_snapshot(event, by_file[f])}
            keys = context_keys(call.repo, state, clauses)
            code = exit_label(event['output'])
            failure = failure_label(event['output']) if clauses and code not in (None, 0) else None
            ident = key + ':' + str(index)
            keys_by_call[ident] = {'keys': keys, 'failure': failure}
            actual = {**runner._call_actuals(call), **loaded.call_actuals[index]}.get('duration_ms')
            if key in manifest['train'] and actual is not None:
                kb.observe(keys, ident=ident, task=key, duration_ms=actual, failure=failure)
        if i % 40 == 0:
            print(json.dumps({'tasks': i + 1, 'samples': len(kb.samples), 'elapsed_s': time.monotonic() - started}), flush=True)
    save('context-keys.json.gz', keys_by_call)
    save('execution-context-kb.json.gz', kb.to_json_obj())
    rows = read('predictions.json.gz')
    before_count = len(kb.samples)
    for r in rows:
        keys = keys_by_call.get(r['id'], {}).get('keys', {})
        correction = kb.predict(keys, r['p50_ms'])
        r['corrected_p50_ms'] = correction['p50'] if correction else r['p50_ms']
        r['correction'] = correction
        r['context_support'] = {kind: {'n': len(kb.groups[kind].get(keys.get(kind), [])),
            'tasks': len({s['task'] for s in kb.groups[kind].get(keys.get(kind), [])})} for kind in ('evidence', 'failure')}
        r['corrected_error_ms'] = abs(r['corrected_p50_ms'] - r['actual_ms']) if r['corrected_p50_ms'] is not None and r['actual_ms'] is not None else None
        if correction:
            assert all(kb.samples[e]['task'] in manifest['train'] for e in correction['evidence_ids'])
    assert before_count == len(kb.samples)
    scored = [r for r in rows if r['absolute_error_ms'] is not None]
    save('context-predictions.json.gz', rows)
    top = sorted(scored, key=lambda r: (-r['corrected_error_ms'], r['id']))[:30]
    save('corrected-worst30.json', [{k: v for k, v in r.items() if k not in ('query_key', 'prediction', 'correction')} for r in top])
    summary = {'training_samples': len(kb.samples), 'corrected_calls': sum(bool(r['correction']) for r in rows),
        'base_mae_s': statistics.mean(r['absolute_error_ms'] for r in scored)/1000,
        'corrected_mae_s': statistics.mean(r['corrected_error_ms'] for r in scored)/1000,
        'improved': sum(r['corrected_error_ms'] < r['absolute_error_ms'] for r in scored),
        'worsened': sum(r['corrected_error_ms'] > r['absolute_error_ms'] for r in scored),
        'test_updates': 0, 'elapsed_s': time.monotonic() - started}
    save('context-summary.json', summary)
    print(json.dumps(summary), flush=True)

if __name__ == '__main__':
    main()
