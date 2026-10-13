"""Independently compare the prepared evaluator with raw production calls."""
from collections import Counter,defaultdict
import gzip
import json
import math
from pathlib import Path
import random
from research.kb_systematic_20261013.study import HERE,EDGES,read,write,estimate_row,progress

def main():
    from tool_resource.runtime_kb import RuntimeToolResourceKB,ClauseResourceKB,ToolCallQuery
    from tool_time.lattice_kb import LatticeTimeKB
    from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from offline.runner import load_task,_safe_recorded_clause
    out=HERE/'results';baseline=Path('baseline');calls=read(out/'calls.json.gz');inv=read(out/'inventory.json')
    selected=random.Random(20261013).sample(calls,128)
    selected += [c for c in calls if any(cl.get('prediction_unavailable_reason') for cl in c['clauses'])]
    ids={c['id'] for c in selected};expected=defaultdict(dict)
    with gzip.open(out/'predictions.jsonl.gz','rt') as f:
        for line in f:
            r=json.loads(line)
            if r['id'] in ids and r['model'] in ('tool','trie','lattice_shrinkage','edge_native'):expected[r['id']][r['model']]=r
    runtime=RuntimeToolResourceKB.from_json_obj(read(baseline/'runtime-tool-resource-kb.json'));runtime.freeze()
    trie=ClauseResourceKB.from_json_obj(read(baseline/'clause-resource-kb.json'));trie.freeze()
    lattice=LatticeTimeKB.from_json_obj(read(baseline/'clause-lattice-time-kb.json'));lattice.freeze()
    edge=EdgeKappaRuntime.from_snapshot(read(baseline/'edge-kappa-kb.json'),frozen=True)
    loaded={};differences=[];checks=0
    for c in selected:
        key=c['task']
        if key not in loaded:loaded[key]=load_task(Path(inv['dataset']),inv['tasks'][key],'MiB')
        task=loaded[key];i=int(c['id'].rsplit(':',1)[1]);call=task.calls[i]
        known=_safe_recorded_clause(call.command,task.call_clauses[i])
        p,d=predict_call_load(runtime=runtime,trie=trie,lattice=lattice,edge_kappa=edge,query=ToolCallQuery(call.repo,call.tool_name,call.command,1e12),edges=load_bucket_edges(EDGES),parsed_clauses=known)
        for name,v in [('tool',p),('trie',d.backends['trie']),('lattice_shrinkage',d.backends['lattice']),('edge_native',d.backends['edge_kappa'])]:
            actual=estimate_row(v.targets['duration_ms']);old=expected[c['id']][name];checks+=1
            match=(actual['p50'] is None and old['p50'] is None or actual['p50'] is not None and old['p50'] is not None and math.isclose(actual['p50'],old['p50'],abs_tol=1e-8,rel_tol=1e-12))
            if not match or actual['predicted_bucket']!=old['predicted_bucket']:
                differences.append({'id':c['id'],'model':name,'production':actual,'study':old})
    original=read(baseline/'original-split.json')
    source={f['path']:f['sha256'] for t in original['tasks'].values() for f in t['files']}
    current={f['path']:f['sha256'] for t in inv['tasks'].values() for f in t['files']}
    write(out/'production-verification.json',{'selected_calls':len(selected),'checks':checks,'differences':differences,
           'original_dataset_sha256_matches':source==current,'source_files':len(source),'current_files':len(current)})
    print('production verification',checks,'differences',len(differences),'dataset hashes',source==current,flush=True)
    assert not differences and source==current

if __name__=='__main__':main()
