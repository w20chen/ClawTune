"""Single-process per-family timing; no experimental cache or oracle."""
import dataclasses, gc, hashlib, json, math, resource, statistics, sys, threading, time
from collections import defaultdict
from pathlib import Path
from tool_resource.runtime_kb import RuntimeToolResourceKB, ClauseResourceKB, CompletedCall, ClauseObservation, ToolCallQuery
from tool_time import lattice_kb as lattice_module
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from clawtune_sidecar.predictors.call_load import compose, summarize
from clawtune_sidecar.prediction_config import load_bucket_edges

family=sys.argv[1]; out=Path('research/kb_cost_20261012')
seed=Path('.runtime/offline/kb-methods-fixed6-20261012-r2/seed')
inputs=json.loads((out/'inputs.json').read_bytes()); samples=inputs['samples']
edges=load_bucket_edges((100,500,2000,10000)); phase={'stage':'restore'}; stop=threading.Event(); start=time.perf_counter()
def heartbeat():
    while not stop.wait(30): print('PROGRESS',family,round(time.perf_counter()-start,2),phase,flush=True)
threading.Thread(target=heartbeat,daemon=True).start()
def stats(values):
    v=sorted(values); return dict(n=len(v),mean_ms=statistics.mean(v),p50_ms=statistics.median(v),
        p95_ms=v[math.ceil(.95*len(v))-1],p99_ms=v[math.ceil(.99*len(v))-1],max_ms=v[-1],total_seconds=sum(v)/1000)
def timed(fn,*args,**kwargs):
    t=time.perf_counter_ns(); result=fn(*args,**kwargs); return result,(time.perf_counter_ns()-t)/1e6
filename={'tool':'runtime-tool-resource-kb.json','trie':'clause-resource-kb.json','edge':'edge-kappa-kb.json'}.get(family,'clause-lattice-time-kb.json')
obj,read_ms=timed(lambda:json.loads((seed/filename).read_bytes()))
if family=='tool': kb,restore_ms=timed(RuntimeToolResourceKB.from_json_obj,obj)
elif family=='trie': kb,restore_ms=timed(ClauseResourceKB.from_json_obj,obj)
elif family=='edge': kb,restore_ms=timed(EdgeKappaRuntime.from_snapshot,obj,frozen=False)
else:
    if family=='lattice0': lattice_module._effective_max_optional_features=lambda observations:0
    kb,restore_ms=timed(lattice_module.LatticeTimeKB.from_json_obj,obj)
if family=='edge': prepare_ms=0
else: _,prepare_ms=timed(kb.freeze)
metadata=dict(family=family,read_decode_ms=read_ms,restore_ms=restore_ms,prepare_ms=prepare_ms,
    ready_seconds=(read_ms+restore_ms+prepare_ms)/1000,
    optional_features=0 if family=='lattice0' else 6 if family=='lattice6' else None)
if family.startswith('lattice'):
    metadata['duration_nodes']=len(kb._resource_states['load:duration_ms'].nodes)
    metadata['raw_observations']=len(kb._observations)
print('READY',metadata,flush=True)
phase['stage']='query'
algorithms=('shrinkage','loso','max_cardinality') if family.startswith('lattice') else (family,)
times=defaultdict(lambda:defaultdict(list)); records=[]
for repeat in range(3):
    for si,row in enumerate(samples):
        phase.update(repeat=repeat,sample=si)
        query=ToolCallQuery(row['repo'],row['tool'],row['command'],1e12)
        for alg in algorithms[repeat%len(algorithms):]+algorithms[:repeat%len(algorithms)]:
            if family=='tool': evidence,match_ms=timed(kb.predict_load_samples,query)
            elif row['reason']: evidence,match_ms=(),0.0
            else:
                kwargs={'algorithm':alg} if family.startswith('lattice') else {}
                evidence,match_ms=timed(kb.predict_load_samples,row['repo'],row['clauses'],1e12,**kwargs)
            backend='lattice' if family.startswith('lattice') else 'edge_kappa' if family=='edge' else family
            if family=='tool':
                prediction,compose_ms=timed(lambda:{target:summarize(target,bounds,'runtime',evidence.get(target,{}).get('values',())) for target,bounds in edges.items()})
            else:
                reason=row['reason'] or next((c.get('prediction_unavailable_reason') for c in row['clauses'] if c.get('prediction_unavailable_reason')),None)
                prediction,compose_ms=timed(compose,backend,evidence,edges,reason=reason,clauses=row['clauses'])
                prediction=prediction.targets
            times[alg]['match'].append(match_ms); times[alg]['compose'].append(compose_ms); times[alg]['combined'].append(match_ms+compose_ms)
            records.append(dict(algorithm=alg,repeat=repeat,sample=si,task=row['task'],index=row['index'],
                match_ms=match_ms,compose_ms=compose_ms,status=prediction['duration_ms'].status,p50=prediction['duration_ms'].p50))
phase['stage']='maintenance'
maintenance={}; update_times=[]; begin_times=[]
if family.startswith('lattice'):
    successor,fork_ms=timed(kb.fork_for_update)
    for i,data in enumerate(inputs['update_clauses']):
        obs=ClauseObservation(**{**data,'argv':tuple(data['argv']),'ts_start':2e12+i*1000,'ts_end':2e12+i*1000+1})
        accepted,ms=timed(successor.observe_completed_clause,obs); assert accepted
        update_times.append(ms)
    maintenance=dict(fork_ms=fork_ms,enqueue_only=stats(update_times),pending=successor.pending_count,
        rebuild_measured_separately_as_prepare=True)
else:
    if family!='edge': kb._frozen=False
    for i,data in enumerate(inputs['update_calls'] if family=='tool' else inputs['update_clauses']):
        data={**data,'ts_start':2e12+i*1000,'ts_end':2e12+i*1000+1}
        t=time.perf_counter_ns()
        if family=='tool': kb.observe_completed_call(CompletedCall(**data)); kb._absorb_completed(data['ts_end']+1)
        elif family=='trie':
            data['argv']=tuple(data['argv']); kb.observe_completed_clause(ClauseObservation(**data)); kb._absorb_completed(data['ts_end']+1)
        else:
            data['argv']=tuple(data['argv']); obs=ClauseObservation(**data)
            _,ms=timed(kb.predict_load_samples,obs.repo,[data],obs.ts_start,call_id=f'cost-{i}')
            begin_times.append(ms); t=time.perf_counter_ns()
            kb.complete_call(f'cost-{i}',[obs],commit_time=obs.ts_end+1)
        update_times.append((time.perf_counter_ns()-t)/1e6)
    maintenance['observe_and_commit']=stats(update_times)
    if begin_times: maintenance['begin_including_prediction']=stats(begin_times)
payload,serialization_ms=timed(kb.to_snapshot if family=='edge' else kb.to_json_obj)
encoded,encoding_ms=timed(json.dumps,payload,separators=(',',':'))
metadata.update(query={alg:{part:stats(v) for part,v in values.items()} for alg,values in times.items()},maintenance=maintenance,
    snapshot_serialize_ms=serialization_ms,snapshot_json_encode_ms=encoding_ms,snapshot_bytes=len(encoded.encode()),
    peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
    elapsed_seconds=time.perf_counter()-start,sample_calls=len(samples),repeats=3,
    scope='prepared clauses; full target evidence retrieval and call composition, excluding parsing, HTTP, per-clause diagnostic assembly and disk persistence; no experimental cache and no oracle')
(out/(family+'.json')).write_text(json.dumps(metadata,indent=2)+'\n')
(out/(family+'-rows.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in records))
stop.set(); print('DONE',json.dumps(metadata),flush=True)
