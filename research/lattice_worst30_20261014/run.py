"""Fresh frozen Lattice evaluation using production ingestion and prediction.

Read-only traces; identical task split to the standard offline runner. No traced
commands are executed. Other prediction backends are empty, not retrained.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import math
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/sidecar/src'))
sys.path.insert(0, str(ROOT))
from offline import runner
from scripts.command_rules.audit_duration_errors import raw_actions
from tool_time.lattice_kb import LatticeTimeKB
from tool_resource.runtime_kb import ToolCallQuery, is_pipeline_dependent_consumer
from command_parser.service import clause_features, observation_features
from clawtune_sidecar.predictors.call_load import predict_call_load
from clawtune_sidecar.prediction_config import load_bucket_edges

OUT = Path(__file__).resolve().parent
DATASET = Path('/mnt/c/Users/29068/Desktop/swe-rebench-original-flat-644-20260904/swe-rebench-original-flat-644-20260904')
START = time.monotonic()
STATUS = {'phase': 'inventory', 'tasks': 0, 'calls': 0}
STOP = threading.Event()

def save(name, obj):
    path = OUT / name
    blob = (json.dumps(obj, ensure_ascii=False, allow_nan=False, default=list) + '\n').encode()
    path.write_bytes(gzip.compress(blob, mtime=0) if path.suffix == '.gz' else blob)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def log():
    print(json.dumps({**STATUS, 'elapsed_s': round(time.monotonic() - START, 2)}), flush=True)

def heartbeat():
    while not STOP.wait(25):
        log()

class Empty:
    def predict_load_samples(self, *args, **kwargs):
        return {}

class Capture:
    def __init__(self, kb):
        self.kb, self.clauses, self.rows = kb, [], []
    def predict_load_samples(self, repo, clauses, ts_start, **kwargs):
        self.clauses = list(clauses)
        self.rows = self.kb.predict_load_samples(repo, clauses, ts_start, **kwargs)
        return self.rows

def finite_duration(row):
    v = row['data'].get('duration_ms')
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0

def raw_record(file, line, row):
    data = row['data']
    return {'file': file, 'line': line, 'action_id': row['action_id'],
            'tool_call_id': data.get('tool_call_id'), 'exit_code': data.get('exit_code'),
            'success': data.get('success'), 'raw_duration_ms': data.get('duration_ms')}

def main():
    threading.Thread(target=heartbeat, daemon=True).start()
    excluded = []
    tasks = runner.inventory(DATASET, 'swe-rebench', excluded)
    manifest = runner.load_or_create_split(tasks, DATASET, 42, ROOT / '.runtime/offline/splits', .8)
    previous = json.loads((ROOT / '.runtime/offline/swe644-current-20261013/split.json').read_text())
    assert manifest['assignment_sha256'] == previous['assignment_sha256']
    assert not set(manifest['train']) & set(manifest['test'])
    save('split.json', manifest)
    hashes = {str(p.relative_to(ROOT)): digest(p)
              for folder in ('offline', 'cold_start', 'services/sidecar/src', 'contracts')
              for p in (ROOT / folder).rglob('*')
              if p.is_file() and p.suffix in ('.py', '.json') and '__pycache__' not in p.parts}
    hashes[str(Path(__file__).relative_to(ROOT))] = digest(Path(__file__))
    save('provenance.json', {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_sha256': hashes, 'dataset': str(DATASET), 'seed': 42, 'train_fraction': .8,
        'max_optional_features': 6, 'rss_unit': 'MiB', 'algorithm': 'shrinkage',
        'ranking': 'descending abs(call p50_ms - qualified actual duration_ms)',
        'test_updates': 0, 'context_correction': False,
        'command': 'PYTHONHASHSEED=42 .runtime/wsl-offline-venv2/bin/python -u research/lattice_worst30_20261014/run.py'})
    clauses, training = [], []
    counts = Counter()
    STATUS['phase'] = 'load_train'
    for key in manifest['train']:
        loaded = runner.load_task(DATASET, tasks[key], 'MiB')
        clauses.extend(loaded.clauses)
        training.extend({'task': key, 'observation': asdict(o)} for o in loaded.clauses)
        counts.update(loaded.counts)
        STATUS['tasks'] += 1
    save('training-observations.json.gz', training)
    STATUS.update(phase='fit', observations=len(clauses))
    log()
    kb = LatticeTimeKB.fit(clauses, max_optional_features=6)
    kb.freeze()
    before = hashlib.sha256(json.dumps(kb.to_json_obj(), sort_keys=True).encode()).hexdigest()
    save('lattice-snapshot.json.gz', kb.to_json_obj())
    edges = load_bucket_edges((100, 500, 2000, 10000))
    STATUS.update(phase='predict', tasks=0, nodes=kb.node_count)
    rows, evidence, query_cache = [], {}, {}
    excluded_calls = Counter()
    for key in manifest['test']:
        loaded = runner.load_task(DATASET, tasks[key], 'MiB')
        raw = [(f, n, r) for f, n, r in raw_actions(DATASET, tasks[key]) if finite_duration(r)]
        assert len(raw) == len(loaded.calls), (key, len(raw), len(loaded.calls))
        for index, call in enumerate(loaded.calls):
            if call.tool_name not in ('exec', 'terminal_exec'):
                excluded_calls['non_shell'] += 1
                continue
            if call.censored:
                excluded_calls['censored_shell'] += 1
                continue
            actuals = {**runner._call_actuals(call), **loaded.call_actuals[index]}
            actual = actuals.get('duration_ms')
            if actual is None:
                excluded_calls['missing_duration_label'] += 1
            known = runner._safe_recorded_clause(call.command, loaded.call_clauses[index])
            cache_key = json.dumps([call.repo, call.tool_name, call.command, known], sort_keys=True)
            if cache_key not in query_cache:
                cap = Capture(kb)
                _, diag = predict_call_load(runtime=Empty(), trie=Empty(), lattice=cap,
                    query=ToolCallQuery(call.repo, call.tool_name, call.command, 1e12),
                    edges=edges, parsed_clauses=known)
                prediction = diag.backends['lattice'].model_dump()
                query_cache[cache_key] = prediction
                evidence[cache_key] = {'clauses': cap.clauses, 'samples': cap.rows}
            prediction = query_cache[cache_key]
            p = prediction['targets']['duration_ms']
            record = {'id': key + ':' + str(index), 'task': key, 'call_index': index,
                'repo': call.repo, 'tool': call.tool_name, 'command': call.command,
                'actual_ms': actual, 'p50_ms': p['p50'], 'p90_ms': p['p90'],
                'absolute_error_ms': abs(p['p50'] - actual) if p['p50'] is not None and actual is not None else None,
                'prediction': p, 'trace': raw_record(*raw[index]), 'query_key': cache_key}
            rows.append(record)
            STATUS['calls'] += 1
        STATUS['tasks'] += 1
    save('predictions.json.gz', rows)
    save('query-evidence.json.gz', evidence)
    scored = [r for r in rows if r['absolute_error_ms'] is not None]
    top = sorted(scored, key=lambda r: (-r['absolute_error_ms'], r['id']))[:30]
    STATUS['phase'] = 'attribute_top30'
    train_features = []
    for item in training:
        from tool_resource.runtime_kb import ClauseObservation
        o = ClauseObservation(**{**item['observation'], 'argv': tuple(item['observation']['argv'])})
        if o.latency_ms is None or o.latency_ms <= 0 or o.in_loop or o.in_subst or is_pipeline_dependent_consumer(o):
            continue
        train_features.append((item['task'], o, observation_features(o).features))
    for rank, row in enumerate(top, 1):
        row['rank'] = rank
        raw = json.loads((DATASET / row['trace']['file']).read_text(encoding='utf-8-sig').splitlines()[row['trace']['line'] - 1])
        row['raw_data'] = raw['data']
        ev = evidence[row['query_key']]
        row['clause_evidence'] = []
        retained = [c for c in ev['clauses'] if not is_pipeline_dependent_consumer(c)]
        for clause, sample in zip(retained, ev['samples']):
            f = clause_features(clause, row['repo'])
            d = sample.get('duration_ms', {})
            selected = frozenset(d.get('context', ())[1:])
            vals = d.get('values', [])
            if not vals:
                continue
            node = kb._resource_states['load:duration_ms'].nodes[selected]
            use_own = selected == f.features and bool(node.own_durations)
            matched = [(task, o, fs) for task, o, fs in train_features
                       if (fs == selected if use_own else selected <= fs)]
            expected = Counter(round(v, 5) for v in vals)
            actual_values = Counter(round(o.latency_ms, 5) for _, o, _ in matched)
            groups = defaultdict(list)
            for task, o, fs in matched:
                groups[(task, o.repo, o.argv, tuple(sorted(fs)))].append(o.latency_ms)
            sources = [{'task': k[0], 'repo': k[1], 'argv': k[2], 'features': k[3], 'values_ms': v}
                       for k, v in groups.items()]
            sources.sort(key=lambda s: (-len(s['values_ms']), s['task'], str(s['argv'])))
            row['clause_evidence'].append({'clause': clause, 'query_features': sorted(f.features),
                'selected_features': sorted(selected), 'exact': selected == f.features,
                'uses_own': use_own, 'count': len(vals), 'p50_ms': statistics.median(vals),
                'min_ms': min(vals), 'max_ms': max(vals), 'values_ms': vals,
                'source_multiset_matches': expected == actual_values, 'training_sources': sources})
        print(json.dumps({'rank': rank, 'id': row['id'], 'actual_s': row['actual_ms']/1000,
            'p50_s': row['p50_ms']/1000, 'error_s': row['absolute_error_ms']/1000,
            'command': row['command']}, ensure_ascii=False), flush=True)
    save('worst30-evidence.json.gz', top)
    save('worst30-summary.json', [{k: v for k, v in r.items() if k not in ('query_key', 'raw_data', 'clause_evidence')} for r in top])
    after = hashlib.sha256(json.dumps(kb.to_json_obj(), sort_keys=True).encode()).hexdigest()
    assert before == after, 'frozen snapshot changed'
    files = {f['path']: f['sha256'] for t in tasks.values() for f in t['files']}
    assert all(digest(DATASET / p) == h for p, h in files.items()), 'source trace changed'
    summary = {'train_tasks': len(manifest['train']), 'test_tasks': len(manifest['test']),
        'files_verified': len(files), 'train_observations': len(clauses), 'training_counts': dict(counts),
        'test_shell_queries': len(rows), 'labeled': sum(r['actual_ms'] is not None for r in rows),
        'predicted': sum(r['p50_ms'] is not None for r in rows), 'scored': len(scored),
        'excluded': dict(excluded_calls), 'test_updates': 0, 'frozen_snapshot_unchanged': before == after,
        'split_sha256': manifest['assignment_sha256'], 'mae_s': statistics.mean(r['absolute_error_ms'] for r in scored)/1000,
        'median_ae_s': statistics.median(r['absolute_error_ms'] for r in scored)/1000,
        'top30_error_share': sum(r['absolute_error_ms'] for r in top)/sum(r['absolute_error_ms'] for r in scored),
        'source_multiset_mismatches': [r['id'] for r in top if any(not c['source_multiset_matches'] for c in r['clause_evidence'])],
        'elapsed_s': time.monotonic() - START}
    save('summary.json', summary)
    STATUS['phase'] = 'complete'
    log()
    print(json.dumps(summary), flush=True)

if __name__ == '__main__':
    try:
        main()
    finally:
        STOP.set()
