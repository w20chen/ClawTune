"""Paired uncertainty, parameter associations, and artifact accounting."""
from collections import Counter,defaultdict
import gzip
import json
import math
from pathlib import Path
import statistics
import numpy as np
from research.kb_systematic_20261013.study import HERE,read,write,metrics
from research.kb_systematic_20261013.optimization import feature_key

def paired(a,b):
    aa={r['id']:r for r in a};bb={r['id']:r for r in b};group=defaultdict(lambda:np.zeros(5));changes=Counter()
    for ident in sorted(aa.keys()&bb.keys()):
        x,y=aa[ident],bb[ident]
        changes['paired_calls']+=1;changes['availability_changed']+=int((x['p50'] is None)!=(y['p50'] is None))
        if x['p50'] is None or y['p50'] is None:continue
        changes['point_changed']+=int(x['p50']!=y['p50']);changes['bucket_changed']+=int(x['predicted_bucket']!=y['predicted_bucket'])
        actual=x['actual_ms'];bucket=x['actual_bucket']
        group[x['task']]+=np.array([1.,int(y['predicted_bucket']==bucket)-int(x['predicted_bucket']==bucket),
            abs(math.log1p(y['p50']/1000)-math.log1p(actual/1000))-abs(math.log1p(x['p50']/1000)-math.log1p(actual/1000)),
            (abs(y['p50']-actual)-abs(x['p50']-actual))/1000,
            int(abs(y['predicted_bucket']-bucket)>=2)-int(abs(x['predicted_bucket']-bucket)>=2)])
    if not group:return {'changes':dict(changes),'common_n':0}
    data=np.array([group[k] for k in sorted(group)]);total=data.sum(0);rng=np.random.default_rng(20261013)
    bootstrap=np.array([data[rng.integers(0,len(data),len(data))].sum(0) for _ in range(2000)])
    result={'changes':dict(changes),'common_n':int(total[0]),'tasks':len(group),'replicates':2000,
            'scope':'paired task bootstrap conditional on the observed repositories; not cross-repository uncertainty'}
    for i,name in enumerate(('accuracy','log_mae','mae_s','severe_error'),1):
        result[name]={'delta':float(total[i]/total[0]),'ci95':np.quantile(bootstrap[:,i]/bootstrap[:,0],[.025,.975]).tolist()}
    return result

def main():
    out=HERE/'results';by=defaultdict(list)
    with gzip.open(out/'predictions.jsonl.gz','rt') as f:
        for l in f:
            r=json.loads(l)
            if r['original_partition']=='test':by[r['model']].append(r)
    ci={}
    for m in ('edge_uniform','edge_shared','edge_shuffled','edge_random','edge_no_weights'):
        ci['baseline_native_vs_'+m]=paired(by['edge_native'],by[m])
    for m in ('tool','trie','lattice_loso','lattice_max_cardinality','edge_native'):
        ci['baseline_lattice_vs_'+m]=paired(by['lattice_shrinkage'],by[m])
    native=read(out/'optimization/native/test.json.gz')
    for p in (out/'optimization').glob('*/test.json.gz'):
        ci['transfer_native_vs_'+p.parent.name]=paired(native,read(p))
    for p in (out/'transfer').glob('*-test.json.gz'):
        ci['shared_native_vs_'+p.name.removesuffix('-test.json.gz')]=paired(native,read(p))
    for partition in ('dev','test'):
        for model in ('edge','lattice'):
            ci[f'llm_{partition}_{model}']=paired(read(out/'llm'/f'{partition}-{model}_original.json.gz'),read(out/'llm'/f'{partition}-{model}_generated.json.gz'))
    write(out/'paired-uncertainty.json',ci)
    # Parameter-value associations, computed on source train/dev only.
    events=[r for r in read(out/'events.json.gz') if r['partition'] in ('train','dev')]
    groups=defaultdict(list)
    for r in events:groups[(r['partition'],r['event']['query']['tool'])].append(r)
    stats=[]
    for (part,tool),rs in groups.items():
        keys=Counter(feature_key(f) for r in rs for f in r['event']['query']['features'] if f.startswith(('opt:','flag:','arg:','mode:','opaque:')))
        for key,count in keys.most_common(20):
            values=defaultdict(list);present=[];absent=[]
            for r in rs:
                features=[f for f in r['event']['query']['features'] if feature_key(f)==key]
                y=r['event']['outcome']['duration_ms'];(present if features else absent).append(y)
                for f in features:values[f].append(y)
            if len(present)<5:continue
            stats.append({'partition':part,'tool':tool,'parameter_key':key,'present_n':len(present),'absent_n':len(absent),
                'present_median_ms':statistics.median(present),'absent_median_ms':statistics.median(absent) if absent else None,
                'log_mean_difference':statistics.mean(math.log1p(v/1000) for v in present)-statistics.mean(math.log1p(v/1000) for v in absent) if absent else None,
                'values':[{'feature':f,'n':len(v),'median_ms':statistics.median(v),'min_ms':min(v),'max_ms':max(v)} for f,v in sorted(values.items(),key=lambda x:-len(x[1]))[:20]]})
    write(out/'parameter-diagnostics.json',{'associations':stats,'limits':'presence and value associations within tool and source repository, not randomized parameter interventions. Opaque/payload features and task workload confound runtime. Interactions cannot be identified by edge-specific kappa alone.'})
    usages=[read(p) for p in (out/'llm').rglob('usage-*.json')]
    requests=list((out/'llm').rglob('request-*.json'));usage=Counter()
    for u in usages:
        for k,v in (u.get('usage') or {}).items():
            if isinstance(v,(int,float)):usage[k]+=v
    write(out/'llm/accounting.json',{'requests_started':len(requests),'responses_recorded':len(usages),
         'http_successes':sum(u.get('http_status')==200 for u in usages),'truncated':sum(u.get('finish_reason')=='length' for u in usages),
         'tokens':dict(usage),'latency_seconds':[u['elapsed_s'] for u in usages],
         'unaccounted_interrupted_requests':len(requests)-len(usages),
         'cost_note':'Actual billing for interrupted requests is unavailable; token-based cost estimate uses published rates, not invoice.'})
    write(out/'selected-dev-after-correction.json',{'primary':{p.parent.name:metrics(read(p)) for p in (out/'optimization').glob('*/dev.json.gz')},
         'transfer':{p.name.removesuffix('-dev.json.gz'):metrics(read(p)) for p in (out/'transfer').glob('*-dev.json.gz')}})

if __name__=='__main__':main()
