"""Verify exact results and summarize the sequential before/after measurements."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent


def read(mode, name):
    return json.loads((root / mode / name).read_text(encoding='utf-8'))


def same_rows(family):
    rows = []
    for mode in ('before-clean', 'after-clean'):
        parsed = [json.loads(line) for line in (root / mode / f'{family}-rows.jsonl').read_text().splitlines()]
        rows.append([{k: v for k, v in row.items() if k not in {'match_ms', 'compose_ms'}} for row in parsed])
    assert json.dumps(rows[0], sort_keys=True) == json.dumps(rows[1], sort_keys=True), family
    return len(rows[0])


models = {}
for family in ('edge', 'lattice6'):
    before, after = [read(mode, family + '.json') for mode in ('before-clean', 'after-clean')]
    assert before['snapshot_sha256'] == after['snapshot_sha256']
    if family == 'lattice6':
        for key in ('state_fingerprints', 'updated_fingerprints', 'legacy_globals'):
            assert before[key] == after[key], key
    models[family] = {
        'identical_prediction_rows': same_rows(family),
        'snapshot_identical': True,
        'before': {k: before[k] for k in ('ready_seconds', 'restore_ms', 'prepare_ms', 'query',
                                        'maintenance', 'peak_rss_mib')},
        'after': {k: after[k] for k in ('ready_seconds', 'restore_ms', 'prepare_ms', 'query',
                                      'maintenance', 'peak_rss_mib')},
    }
    if family == 'lattice6':
        models[family]['initial_and_updated_node_statistics_identical'] = True
        for label, obj in (('before', before), ('after', after)):
            models[family][label].update({k: obj[k] for k in ('update_prepare_ms', 'update_publish_ms',
                                                             'ready_peak_rss_mib', 'duration_nodes',
                                                             'raw_observations')})

before, after = [read(mode, 'flush.json') for mode in ('before-clean', 'after-clean')]
assert before['committed'] == after['committed']
assert before['snapshot_sha256'] == after['snapshot_sha256']
synthetic = [root / ('synthetic-' + mode + '.json') for mode in ('before', 'after')]
assert synthetic[0].read_bytes() == synthetic[1].read_bytes()
summary = {
    'models': models,
    'managed_flush': {'before_seconds': before['flush_seconds'], 'after_seconds': after['flush_seconds'],
                      'all_four_committed_snapshots_identical': True,
                      'scope': before['scope']},
    'synthetic_resources_and_partial_causal_updates_identical': True,
    'scope': 'Same seed and 128 fixed calls; three query passes per selector. Sequential processes with PYTHONHASHSEED=42. One full-build and one 32-observation rebuild per measured process; separate full managed flush run. Preliminary runs excluded from timing comparison.',
    'limits': 'Single-machine measurements, not confidence intervals or HTTP latency. Peak RSS is process high-water mark. Exact caches trade bounded additional memory and fork copying for avoided computation. Lattice still performs full coverage aggregation and shrinkage traversal.',
}
(root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'edge_rows': models['edge']['identical_prediction_rows'],
                  'lattice_rows': models['lattice6']['identical_prediction_rows'],
                  'equivalence': 'passed'}))
