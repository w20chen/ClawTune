"""Stratify frozen predictions by supported tool scope; do not refit any model."""
from collections import Counter, defaultdict
import gzip
import json
from research.kb_systematic_20261013.study import HERE, read, write, metrics
from research.kb_systematic_20261013.analysis import paired


def main():
    out = HERE / 'results'
    calls = {r['id']: r for r in read(out / 'calls.json.gz')}
    models = defaultdict(list)
    with gzip.open(out / 'predictions.jsonl.gz', 'rt') as stream:
        for line in stream:
            row = json.loads(line)
            models[row['model']].append(row)
    names = ('tool', 'trie', 'lattice_shrinkage', 'edge_native')
    result = {'scope_definition': 'shell: tool in {exec, terminal_exec}; no conditioning on parse success',
              'tools': dict(Counter(r['tool'] for r in calls.values())), 'scopes': {}}
    for partition in ('all', 'original_test'):
        chosen = {name: [r for r in rows if partition == 'all' or r['original_partition'] == 'test']
                  for name, rows in models.items()}
        for scope in ('shell', 'non_shell'):
            rows = {name: [r for r in rr if (r['tool'] in ('exec', 'terminal_exec')) == (scope == 'shell')]
                    for name, rr in chosen.items()}
            common = set.intersection(*[{r['id'] for r in rr if r['p50'] is not None} for rr in rows.values()])
            result['scopes'][partition + '/' + scope] = {
                'metrics': {name: metrics(rows[name]) for name in names},
                'common_all_11_n': len(common),
                'common_all_11': {name: metrics([r for r in rows[name] if r['id'] in common]) for name in names},
                'unavailable_reasons': {name: dict(Counter(r.get('unavailable_reason') for r in rows[name] if r['p50'] is None)) for name in names},
                'actual_buckets': dict(Counter(r['actual_bucket'] for r in rows['tool']))}
    heldout = {name: [r for r in models[name] if r['original_partition'] == 'test' and r['tool'] in ('exec', 'terminal_exec')] for name in names}
    result['paired_shell_heldout'] = {name: paired(heldout[name], heldout['tool']) for name in names if name != 'tool'}
    write(out / 'reassessment-scope.json', result)
    for key, group in result['scopes'].items():
        print(key, 'common=', group['common_all_11_n'])
        for name, row in group['metrics'].items():
            print(name, {k: row[k] for k in ('n', 'predicted', 'coverage', 'accuracy', 'log_mae')})
        print('reasons', group['unavailable_reasons'])
    print('tools', result['tools'])
    print('paired', result['paired_shell_heldout'])


if __name__ == '__main__':
    main()
