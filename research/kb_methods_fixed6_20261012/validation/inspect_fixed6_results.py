"""Print paired changes and compact result tables from the archived run."""
import gzip
import json
import statistics
from pathlib import Path

out = Path('research/kb_methods_fixed6_20261012')
old = Path('research/kb_methods_evaluation_20261012')
summary = json.loads((out / 'summary.json').read_bytes())
read_rows = lambda p: [json.loads(x) for x in gzip.decompress(p.read_bytes()).splitlines()]
previous = read_rows(old / 'model-predictions.jsonl.gz')
current = read_rows(out / 'model-predictions.jsonl.gz')
order = ('tool', 'trie', 'edge_kappa', 'lattice', 'lattice_loso', 'lattice_max_cardinality', 'lattice_oracle')
print('MAIN: model, available, coverage%, accuracy%, macro_recall%, MAE_s, common_MAE_s')
for name in order:
    x = summary['shell_models'][name]
    print(name, x['predicted'], *(round(100*x[k], 2) for k in ('coverage', 'accuracy_available', 'macro_recall_all')),
          round(summary['shell_duration_metrics'][name]['mae']/1000, 3),
          round(summary['common_duration_mae_ms'][name]/1000, 3))
print('BUCKET P/R:')
for name in order:
    print(name, ' | '.join(f"{100*b['precision']:.2f}/{100*b['recall_all']:.2f}" for b in summary['shell_models'][name]['per_bucket']))
print('COMMON', summary['common_count'])
print('ALL CALLS', {m: (v['eligible'], v['predicted'], v['accuracy_available']) for m,v in summary['models'].items()})
print('UNAVAILABLE', {m: v['unavailable_reasons'] for m,v in summary['shell_duration_metrics'].items()})
paired = {}
for name in ('lattice', 'lattice_loso', 'lattice_max_cardinality'):
    a = [r for r in previous if r['model'] == name and r['target'] == 'duration_ms']
    b = [r for r in current if r['model'] == name and r['target'] == 'duration_ms']
    assert [(r['task'], r['tool'], r['actual']) for r in a] == [(r['task'], r['tool'], r['actual']) for r in b]
    pairs = [(x,y) for x,y in zip(a,b) if x['status'] == y['status'] == 'available']
    paired[name] = dict(count=len(pairs), old_mae_ms=statistics.mean(abs(x['p50']-x['actual']) for x,y in pairs),
        new_mae_ms=statistics.mean(abs(y['p50']-y['actual']) for x,y in pairs),
        improved=sum(abs(y['p50']-y['actual']) < abs(x['p50']-x['actual'])-1e-6 for x,y in pairs),
        worsened=sum(abs(y['p50']-y['actual']) > abs(x['p50']-x['actual'])+1e-6 for x,y in pairs),
        lost=sum(x['status']=='available' and y['status']!='available' for x,y in zip(a,b)),
        gained=sum(x['status']!='available' and y['status']=='available' for x,y in zip(a,b)))
print('BEFORE/AFTER', json.dumps(paired, indent=2))
(out / 'paired_changes.json').write_text(json.dumps(paired, indent=2)+'\n', encoding='utf8')
report = json.loads(gzip.decompress((out / 'report.json.gz').read_bytes()))
print('NON-DURATION', json.dumps({m: [{k:v for k,v in r.items() if k in ('target','eligible','predicted','coverage','mae')}
    for r in value['metrics'] if r['target'] != 'duration_ms'] for m,value in report['models'].items()}, indent=2))
