"""Audit source identity, before/after predictions, full-state equality and timings."""
import hashlib
import json
import math
from pathlib import Path

base = Path(__file__).resolve().parent
root = base.parents[1]
old = base.parent / 'kb_online_cost_20261012'


def read(path):
    return json.loads(path.read_bytes())


def result(host, case):
    # Edge production files did not change after the first completed matrix.
    folder = 'phase1' if case.startswith('edge-') else 'results'
    return base / folder / host / (case + '.json')


def queries(path):
    return [r for line in path.with_name(path.stem + '-rows.jsonl').read_text().splitlines()
            if (r := json.loads(line))['kind'] == 'query']


def compare_queries(a, b):
    left, right = queries(a), queries(b)
    assert len(left) == len(right)
    for x, y in zip(left, right):
        assert all(x[k] == y[k] for k in ('repeat', 'sample', 'status'))
        assert x['p50'] == y['p50'] or math.isclose(x['p50'], y['p50'], rel_tol=1e-9, abs_tol=1e-6)
    return len(left)


manifest = read(base / 'source_sha256.json')
production = {p: h for p, h in manifest.items() if p.startswith(('services/', 'contracts/'))}
for path, digest in production.items():
    assert hashlib.sha256((root / path).read_bytes()).hexdigest() == digest, path
phase1 = read(base / 'source_phase1.json')
edge_files = [p for p in production if p.startswith(('services/sidecar/src/edge_kappa_kb/',
              'services/sidecar/src/kb_common/')) or p in (
              'services/sidecar/src/clawtune_sidecar/predictors/edge_kappa.py',
              'services/sidecar/src/clawtune_sidecar/predictors/tool_resource.py',
              'services/sidecar/src/clawtune_sidecar/predictors/call_load.py')]
assert all(manifest[p] == phase1[p] for p in edge_files)

checks, summary = [], {}
for host in ('notebook', 'kunpeng'):
    summary[host] = {}
    for case in ('lattice-6', 'lattice-2', 'edge-6', 'edge-2', 'pair-2'):
        path = result(host, case)
        data = read(path)
        supervised = read(path.with_name(case + '-supervisor.json'))
        assert data['phase']['stage'] == 'done'
        assert supervised['returncode'] == 0 and supervised['termination'] is None
        assert data['corpus_sha256'] == read(result('notebook', 'lattice-6'))['corpus_sha256']
        assert data['inputs_sha256'] == read(result('notebook', 'lattice-6'))['inputs_sha256']
        entry = {'source': str(path.relative_to(root)), 'peak_rss_mib': data['peak_rss_mib']}
        if case.startswith('lattice-'):
            before = read(old / host / (case + '.json'))
            def total(update):
                return sum(update[k] for k in ('fork_ms', 'prepare_ms', 'activate_ms')) + update['enqueue']['total_ms']
            entry.update(initial_ready_s=data['ready_s'],
                before_update_total_ms={n: total(v) for n, v in before['updates'].items()},
                after_update_total_ms={n: total(v) for n, v in data['updates'].items()},
                first_queries_after_updates=data['first_queries_after_each_update_batch'],
                query_and_composition=data['read_only_combined'])
        elif case.startswith('edge-'):
            entry.update(initial_ready_s=data['ready_s'], graph_nodes=data['graph_nodes'],
                graph_edges=data['graph_edges'], online_begin_plus_compose=data['online_begin_plus_compose'],
                online_complete=data['online_complete'], detached_snapshots=data['detached_snapshots'],
                snapshot_cold_then_reused_ms=data['stream_snapshot_cold_then_reused_ms'],
                snapshot_bytes=data['stream_snapshot_bytes'])
        else:
            entry.update({k: data[k] for k in ('pair_baseline', 'pair_during_update',
                'pair_during_update_lock_wait', 'update_visible_s', 'update_durable_s',
                'published_added_observations')})
            assert data['published_added_observations'] == 32
        if case in ('lattice-6', 'lattice-2', 'edge-2'):
            checks.append({'host': host, 'case': case, 'before_after_matching_queries':
                           compare_queries(old / host / (case + '.json'), path)})
        summary[host][case] = entry
for case in ('lattice-6', 'lattice-2', 'edge-6', 'edge-2'):
    checks.append({'case': case, 'cross_host_matching_queries':
                   compare_queries(result('notebook', case), result('kunpeng', case))})

summary['kunpeng']['pair-6'] = {'status': 'stopped_during_restore_to_finish_at_user_request',
                              'online_concurrent_latency': None}
reference = read(base / 'results/kunpeng/full-state-r2/reference-fingerprint.json')
incremental = read(base / 'results/kunpeng/full-state-r2/incremental-fingerprint.json')
assert reference == incremental
edge_reference = read(base / 'results/notebook/edge-reference-fingerprint.json')
assert edge_reference == read(base / 'results/notebook/edge-incremental-fingerprint.json')
report = {'production_source_files_matched': len(production),
          'edge_phase1_files_unchanged': edge_files, 'checks': checks,
          'r2_full_corpus_all_node_fields_and_order_exactly_equal': True,
          'edge_independent_baseline_exact_event_sequences': len(edge_reference),
          'fingerprints': 'results/kunpeng/full-state-r2/{reference,incremental}-fingerprint.json',
          'full_state_scope': 'Full-corpus fingerprints used source_phase2.json. Final source additionally corrects '
                              'heap-batch generation order and exact-query cold-start metadata; final differential tests passed. '
                              'A second full-corpus fingerprint run on final source was not started after the user requested completion.',
          'targeted_pytest': {'passed': 355, 'warnings': 9, 'seconds': 43.36},
          'scope': 'Prepared clauses; no HTTP, raw parser, compatibility diagnostic assembly or StateStore checkpoint SLA. '
                   'One batch per size; query p95 is over recorded calls, not repeated whole experiments. '
                   'Pair-2 includes real shared writer and all four backends. Pair-6 concurrent latency remains unmeasured. '
                   'update_durable_s is writer/barrier completion including atomic replacement, not a power-loss/fsync guarantee.'}
(base / 'verification.json').write_text(json.dumps(report, indent=2))
(base / 'summary.json').write_text(json.dumps(summary, indent=2))
print(json.dumps(report, indent=2))
