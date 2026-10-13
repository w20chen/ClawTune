import gzip
import json
import resource
import time
from pathlib import Path

from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import compose
from offline.lattice_oracle import LatticeOracle
from tool_time import lattice_kb as module

root=Path.cwd()
out=root/'research/kb_methods_evaluation_20261012/fixed6_validation'
out.mkdir(parents=True,exist_ok=True)
source=root/'.runtime/offline/kb-methods-20261012/seed/clause-lattice-time-kb.json'
old=json.loads(source.read_bytes())
original=module.build_resource_states
def measured(*args,**kwargs):
    start=time.monotonic()
    print('resource view start',kwargs,flush=True)
    result=original(*args,**kwargs)
    print('resource view done',round(time.monotonic()-start,2),{k:len(v.nodes) for k,v in result.items()},flush=True)
    return result
module.build_resource_states=measured
start=time.monotonic()
kb=module.LatticeTimeKB.from_json_obj(old)
kb.freeze()
result=dict(build_seconds=time.monotonic()-start,peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
    observations=kb.observation_count,duration_nodes=len(kb._resource_states['load:duration_ms'].nodes),
    node_generation=kb.to_json_obj()['node_generation'],queries=[])
assert kb.observation_count==len(old['observations'])
assert result['node_generation']['max_optional_features']==6
assert result['node_generation']['optional_feature_policy']=='fixed'
print('BUILD',result,flush=True)
(out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
cases=json.loads(gzip.decompress((root/'research/kb_methods_evaluation_20261012/shrinkage_worst30/cases.json.gz').read_bytes()))
edges=load_bucket_edges((100,500,2000,10000))
for c in cases:
    if c['rank'] not in (8,30):
        continue
    clauses=[dict(argv=d['argv'],command_features=d['features']) for d in c['clauses']]
    repo=json.loads(next(f[8:] for f in c['clauses'][0]['features']['features'] if f.startswith('context:["repo",')))[1]
    actual=c['actual_ms']
    oracle=LatticeOracle(kb,actual,edges)
    choices=kb.duration_candidate_samples(repo,clauses,1e12)
    print('QUERY',c['rank'],'choices',[len(x) for x in choices],flush=True)
    row=dict(rank=c['rank'],actual_ms=actual,candidate_counts=[len(x) for x in choices])
    for algorithm in (*module.LATTICE_TIME_ALGORITHMS,'oracle'):
        start=time.monotonic()
        evidence=(oracle.predict_load_samples(repo,clauses,1e12) if algorithm=='oracle'
                  else kb.predict_load_samples(repo,clauses,1e12,algorithm=algorithm))
        prediction=compose('lattice',evidence,edges,clauses=clauses).targets['duration_ms']
        row[algorithm]=dict(p50=prediction.p50,status=prediction.status,seconds=time.monotonic()-start)
    assert abs(row['oracle']['p50']-actual)<=abs(row['shrinkage']['p50']-actual)+1e-6
    result['queries'].append(row)
    print('QUERY RESULT',row,flush=True)
result['peak_rss_mib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
(out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
