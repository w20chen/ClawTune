import gzip
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
def read(name):
    p=OUT/name
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes())

rows=read('predictions.json.gz')
top=read('worst30-evidence.json.gz')
audit=read('training-output-audit.json.gz')
review=read('review.json')
provenance=read('provenance.json')
assert len(top)==30 and all(r['tool'] in ('exec','terminal_exec') for r in rows)
assert all(a['absolute_error_ms']>=b['absolute_error_ms'] for a,b in zip(top,top[1:]))
assert all(c['source_multiset_matches'] for r in top for c in r['clause_evidence'])
assert all(a['missing_source']==0 for a in audit)
changed=[f for f,h in provenance['source_sha256'].items() if hashlib.sha256((ROOT/f).read_bytes()).hexdigest()!=h]
assert not changed,changed
old=[json.loads(l) for l in (ROOT/'.runtime/offline/swe644-current-20261013/model-predictions.jsonl').read_text().splitlines()]
old=[r for r in old if r['model']=='lattice' and r['target']=='duration_ms' and r['tool'] in ('exec','terminal_exec')]
new=[r for r in rows if r['actual_ms'] is not None]
assert len(old)==len(new),(len(old),len(new))
differences=[]
for a,b in zip(old,new):
    assert a['task']==b['task'] and math.isclose(a['actual'],b['actual_ms'],rel_tol=1e-10)
    if a['p50']!=b['p50_ms'] and not(a['p50'] is not None and b['p50_ms'] is not None and math.isclose(a['p50'],b['p50_ms'],rel_tol=1e-10,abs_tol=1e-6)):
        differences.append(b['id'])
html=(OUT/'report.html').read_text(encoding='utf8')
assert sum(f'id="case{i}"' in html for i in range(1,31))==30
result={'shell_rows':len(rows),'ranked_cases':len(top),'training_clause_sources_checked':len(audit),
    'raw_source_missing':0,'source_files_unchanged':True,
    'previous_full_evaluator_shell_duration_rows_compared':len(old),'differences_from_previous_run':differences,
    'case_anchors':30,'categories':dict(Counter(review[r['id']]['category'] for r in top)),
    'top30_unique_tasks':len({r['task'] for r in top})}
(OUT/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps(result,ensure_ascii=False,indent=2))
for r in top:
    print(r['rank'],r['id'].split('__')[1],*(round(r[k]/1000,2) for k in ('actual_ms','p50_ms','absolute_error_ms')))
