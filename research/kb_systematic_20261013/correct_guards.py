"""Replay the production rejection guard missed by the first study adapter.

No model weights, observations or test labels change. All original files are
archived once; metrics are recalculated from actual unavailable composer output.
"""
from collections import defaultdict
import gzip
import json
from pathlib import Path
import shutil
from research.kb_systematic_20261013.study import HERE,EDGES,read,write,metrics,estimate_row

def main():
    from clawtune_sidecar.predictors.call_load import compose
    from clawtune_sidecar.prediction_config import load_bucket_edges
    out=HERE/'results';calls=read(out/'calls.json.gz');bad={};edges=load_bucket_edges(EDGES)
    for c in calls:
        reason=next((cl.get('prediction_unavailable_reason') for cl in c['clauses'] if cl.get('prediction_unavailable_reason')),None)
        if reason:bad[c['id']]=reason;c['parse_reason']=reason
    write(out/'calls.json.gz',calls)
    archive=out/'adapter-before-guard-fix';archive.mkdir(exist_ok=True)
    affected=[]
    def correct(rows,name):
        count=0
        for r in rows:
            if r.get('id') not in bad or r.get('model')=='tool':continue
            backend=r.get('backend','edge_kappa')
            if backend=='runtime':continue
            prediction=estimate_row(compose(backend,(),edges,reason=bad[r['id']]).targets['duration_ms'])
            was=r.get('p50') is not None;r.update(prediction);count+=int(was)
            if 'decisions' in r:r['decisions']=[]
        affected.append({'file':name,'previously_available_rejected':count})
        return rows
    p=out/'predictions.jsonl.gz'
    if not (archive/p.name).exists():shutil.copy2(p,archive/p.name)
    with gzip.open(p,'rt') as f:rows=correct([json.loads(l) for l in f],p.name)
    with gzip.open(p,'wt',encoding='utf8') as f:
        for r in rows:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    modelrows=defaultdict(list)
    for r in rows:modelrows[r['model']].append(r)
    summary={}
    split=read(out/'optimization-split.json')['tasks']
    for scope in ('all','original_test','dev'):
        selected={n:[r for r in rr if scope=='all' or scope=='original_test' and r['original_partition']=='test' or scope=='dev' and split[r['task']]=='dev'] for n,rr in modelrows.items()}
        common=set.intersection(*({r['id'] for r in rr if r['p50'] is not None} for rr in selected.values()))
        summary[scope]={'full':{n:metrics(rr) for n,rr in selected.items()},'common_n':len(common),
                        'common':{n:metrics([r for r in rr if r['id'] in common]) for n,rr in selected.items()}}
    write(out/'baseline-metrics.json',summary)
    for pattern in ('optimization/*/dev.json.gz','optimization/*/test.json.gz','transfer/*-dev.json.gz','transfer/*-test.json.gz','calibration/*.json.gz','llm/*-edge_*.json.gz','llm/*-lattice_*.json.gz'):
        for p in out.glob(pattern):
            target=archive/p.relative_to(out);target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists():shutil.copy2(p,target)
            write(p,correct(read(p),str(p.relative_to(out))))
    # Lock choices remain those made on development data before viewing test.
    test={p.parent.name:read(p) for p in (out/'optimization').glob('*/test.json.gz')}
    if test:
        common=set.intersection(*({r['id'] for r in rr if r['p50'] is not None} for rr in test.values()))
        write(out/'optimization-test.json',{'selected':read(out/'selection-lock.json')['best'],'all':{n:metrics(rr) for n,rr in test.items()},
              'common_n':len(common),'common':{n:metrics([r for r in rr if r['id'] in common]) for n,rr in test.items()}})
    test={p.name.removesuffix('-test.json.gz'):read(p) for p in (out/'transfer').glob('*-test.json.gz')}
    if test:write(out/'transfer-test.json',{'selected':read(out/'transfer-lock.json')['selected'],'test':{n:metrics(rr) for n,rr in test.items()}})
    calibration=[]
    for p in sorted((out/'calibration').glob('*.json.gz')):
        name,n=p.name.removesuffix('.json.gz').rsplit('-',1);calibration.append({'name':name,'feedback':int(n),'metrics':metrics(read(p))})
    if calibration:write(out/'calibration-summary.json',calibration)
    if (out/'llm/evaluation.json').exists():
        d=read(out/'llm/evaluation.json')
        for partition in ('dev','test'):
            affected_ids={r['id'] for r in read(out/'llm'/f'{partition}-parse-changes.json.gz')}
            for name in d[partition]['all']:
                rr=read(out/'llm'/f'{partition}-{name}.json.gz');d[partition]['all'][name]=metrics(rr)
                d[partition]['affected'][name]=metrics([r for r in rr if r['id'] in affected_ids])
        write(out/'llm/evaluation.json',d)
    write(out/'adapter-guard-correction.json',{'calls':bad,'changed_outputs':affected,'cause':'study adapter omitted clause prediction_unavailable_reason at call composition; production correctly rejects these commands',
          'selection_choices_retained':True,'original_outputs_archive':'adapter-before-guard-fix'})

if __name__=='__main__':main()
