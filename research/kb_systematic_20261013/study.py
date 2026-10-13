"""Frozen production-path evaluation, with trace identities and repository isolation."""
from __future__ import annotations
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import statistics
import time

HERE = Path(__file__).resolve().parent
EDGES = (100, 500, 2000, 10000)

def read(p):
    p = Path(p)
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix == '.gz' else p.read_bytes())

def write(p, obj):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False, allow_nan=False, default=list) + '\n').encode()
    p.write_bytes(gzip.compress(data, mtime=0) if p.suffix == '.gz' else data)

def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def fingerprint(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=list).encode()).hexdigest()

def progress(stage, **kw):
    print(json.dumps({'stage': stage, **kw}), flush=True)

class Empty:
    def predict_load_samples(self, *args, **kwargs): return []

class Capture(Empty):
    def predict_load_samples(self, repo, clauses, *args, **kwargs):
        self.clauses = clauses
        return []

def prepare(dataset, out):
    from offline.runner import inventory, load_task, _call_actuals, _safe_recorded_clause
    from scripts.command_rules.audit_duration_errors import raw_actions
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from tool_resource.runtime_kb import ToolCallQuery
    excluded=[]; tasks=inventory(dataset, 'swe-rebench', excluded)
    repos=sorted({v['group'] for v in tasks.values()}, key=lambda r: hashlib.sha256(('20261013:'+r).encode()).hexdigest())
    # Commit assignment before any outcome inspection or model selection.
    nr=len(repos); membership={r: ('train' if i<int(.6*nr) else 'dev' if i<int(.8*nr) else 'test') for i,r in enumerate(repos)}
    split={'seed':20261013,'unit':'repository','repositories':membership,
           'tasks':{k:membership[v['group']] for k,v in tasks.items()}}
    write(out/'optimization-split.json',split)
    counts=Counter(); calls=[]; observations=[]; parser_counts=Counter(); tool_counts=Counter()
    for ti,(key,task) in enumerate(sorted(tasks.items())):
        loaded=load_task(dataset,task,'MiB'); counts.update(loaded.counts)
        raw=list(raw_actions(dataset,task))
        eligible_raw=[(f,n,r) for f,n,r in raw if isinstance(r['data'].get('duration_ms'),(int,float))
                      and not isinstance(r['data'].get('duration_ms'),bool) and math.isfinite(r['data']['duration_ms']) and r['data']['duration_ms']>=0]
        assert len(eligible_raw)==len(loaded.calls),(key,len(eligible_raw),len(loaded.calls))
        counts['raw_tool_actions']+=len(raw); counts['loaded_calls']+=len(loaded.calls)
        for oi,o in enumerate(loaded.clauses):
            observations.append({'task':key,'repo_group':task['group'],'partition':membership[task['group']],'observation':asdict(o)})
        for i,c in enumerate(loaded.calls):
            if c.censored: counts['excluded_censored_calls']+=1; continue
            actual={**_call_actuals(c), **(loaded.call_actuals[i] if i<len(loaded.call_actuals) else {})}.get('duration_ms')
            if actual is None or not math.isfinite(actual) or actual<0: counts['excluded_no_duration']+=1;continue
            f,n,r=eligible_raw[i]; cap=Capture();cap.clauses=()
            known=_safe_recorded_clause(c.command,loaded.call_clauses[i] if i<len(loaded.call_clauses) else ())
            _,diag=predict_call_load(runtime=Empty(),trie=cap,lattice=Empty(),query=ToolCallQuery(c.repo,c.tool_name,c.command,1e12),
                                    edges=load_bucket_edges(EDGES),parsed_clauses=known)
            reason=diag.backends['trie'].targets['duration_ms'].unavailable_reason
            # Capture not invoked if shell parsing rejected; otherwise no evidence is expected.
            parse_reason=reason if not cap.clauses and reason not in ('no_compatible_evidence','no_clause_evidence') else next(
                (cl.get('prediction_unavailable_reason') for cl in cap.clauses if cl.get('prediction_unavailable_reason')), None)
            record={'id':key+':'+str(i),'task':key,'repo':c.repo,'repo_group':task['group'], 'partition':membership[task['group']],
                    'tool':c.tool_name,'command':c.command,'actual_ms':actual,'actual_bucket':bisect_right(EDGES,actual),
                    'trace':{'file':f,'line':n,'action_id':r['action_id'],'tool_call_id':r['data'].get('tool_call_id')},
                    'clauses':cap.clauses,'parse_reason':parse_reason,
                    'exit_code':r['data'].get('exit_code'),'success':r['data'].get('success')}
            calls.append(record)
            for cl in cap.clauses:
                cf=cl.get('command_features',{});parser_counts[cf.get('status','missing')]+=1
                tool_counts[cf.get('tool',cl.get('bin','unknown'))]+=1
        if ti%40==0: progress('prepare',tasks=ti,calls=len(calls),clauses=len(observations))
    write(out/'calls.json.gz',calls);write(out/'observations.json.gz',observations)
    write(out/'inventory.json',{'tasks':tasks,'excluded_files':excluded,'counts':dict(counts),'valid_calls':len(calls),
                              'observations':len(observations),'parser_counts':dict(parser_counts),'tools':dict(tool_counts),
                              'files':sum(len(t['files']) for t in tasks.values()),'repositories':len(repos),
                              'python':platform.python_version(),'platform':platform.platform(),'dataset':str(dataset)})

def metrics(rows):
    n=len(rows); avail=[r for r in rows if r['p50'] is not None and math.isfinite(r['p50']) and r['p50']>=0]
    per=[]
    for b in range(5):
        support=sum(r['actual_bucket']==b for r in rows);covered=[r for r in avail if r['actual_bucket']==b]
        correct=sum(r['predicted_bucket']==b for r in covered)
        per.append({'bucket':b,'support':support,'covered':len(covered),'correct':correct,
                    'recall_all':correct/support if support else None,'recall_covered':correct/len(covered) if covered else None})
    abses=[abs(r['p50']-r['actual_ms'])/1000 for r in avail]
    logs=[abs(math.log1p(r['p50']/1000)-math.log1p(r['actual_ms']/1000)) for r in avail]
    correct=sum(r['predicted_bucket']==r['actual_bucket'] for r in avail)
    factors=sum(.5*r['actual_ms']<=r['p50']<=2*r['actual_ms'] for r in avail)
    recalls=[r['recall_all'] for r in per if r['recall_all'] is not None]
    covered_recalls=[r['recall_covered'] for r in per if r['recall_covered'] is not None]
    return {'n':n,'predicted':len(avail),'coverage':len(avail)/n if n else None,
            'accuracy':correct/len(avail) if avail else None,'accuracy_all':correct/n if n else None,
            'macro_recall':statistics.mean(covered_recalls) if covered_recalls else None,
            'macro_recall_all':statistics.mean(recalls) if recalls else None,'per_bin':per,
            'mae_s':statistics.mean(abses) if abses else None,'median_ae_s':statistics.median(abses) if abses else None,
            'log_mae':statistics.mean(logs) if logs else None,'factor2':factors/len(avail) if avail else None,
            'severe_error':sum(abs(r['predicted_bucket']-r['actual_bucket'])>=2 for r in avail)/len(avail) if avail else None}

def estimate_row(estimate):
    d=estimate.model_dump();p=d['buckets']['probabilities']
    return {**d,'predicted_bucket':max(range(len(p)),key=lambda i:(p[i],-i)) if p and d['p50'] is not None else None,
            'point_bucket':bisect_right(EDGES,d['p50']) if d['p50'] is not None else None}

def edge_evidence(kb, clauses, repo, mode='native'):
    from command_parser.service import clause_features
    from tool_time.edge_kappa_adapter import feature_query
    from tool_resource.runtime_kb import is_pipeline_dependent_consumer
    result=[]; diagnostic=[]
    for cl in clauses:
        if is_pipeline_dependent_consumer(cl): continue
        q=feature_query(clause_features(cl,repo))
        dist=kb.predict_duration(q); pred=dist.bucket_prediction
        values,weights=dist.values_ms,dist.weights
        if mode=='no_weights': weights=tuple(1.0 for _ in values)
        result.append({'duration_ms':{'values':values,'weights':weights,'evidence_count':dist.evidence_count,
                     'unavailable_reason':dist.unavailable_reason,'context':['edge_kappa:'+pred.node_id]}})
        diagnostic.append(asdict(pred))
    return result,diagnostic

def set_weights(kb, mode, original, seed=42):
    rng=random.Random(seed)
    for child,parents in sorted(kb.graph.parents.items()):
        if not parents:continue
        vals=[original[(p,child)] for p in parents]
        if mode=='uniform':vals=[math.log(kb.k0/len(parents))]*len(parents)
        elif mode=='shared':vals=[math.log(sum(math.exp(v) for v in vals)/len(vals))]*len(vals)
        elif mode=='shuffled':rng.shuffle(vals)
        elif mode=='random':
            w=[math.exp(rng.uniform(-2,2)) for _ in parents];vals=[math.log(kb.k0*x/sum(w)) for x in w]
        for p,v in zip(parents,vals):kb.edges[(p,child)].log_kappa=v

def frozen(out, baseline):
    from tool_resource.runtime_kb import RuntimeToolResourceKB,ClauseResourceKB,ToolCallQuery
    from tool_time.lattice_kb import LatticeTimeKB
    from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
    from clawtune_sidecar.predictors.call_load import compose,summarize
    from clawtune_sidecar.prediction_config import load_bucket_edges
    calls=read(out/'calls.json.gz');original_split=read(baseline/'original-split.json')
    hashes={p.name:digest(p) for p in baseline.glob('*.json')}
    runtime=RuntimeToolResourceKB.from_json_obj(read(baseline/'runtime-tool-resource-kb.json'));runtime.freeze()
    trie=ClauseResourceKB.from_json_obj(read(baseline/'clause-resource-kb.json'));trie.freeze()
    lattice=LatticeTimeKB.from_json_obj(read(baseline/'clause-lattice-time-kb.json'));lattice.freeze()
    edge=EdgeKappaRuntime.from_snapshot(read(baseline/'edge-kappa-kb.json'),frozen=True)
    models={'tool':runtime,'trie':trie,'lattice':lattice,'edge':edge}
    state_before={k:fingerprint(v.to_snapshot() if k=='edge' else v.to_json_obj()) for k,v in models.items()}
    weights={k:e.log_kappa for k,e in edge.kb.edges.items()};edges=load_bucket_edges(EDGES)
    import copy
    native_edges=edge.kb.edges
    weight_maps={'native':native_edges,'no_weights':native_edges}
    for mode in ('uniform','shared','shuffled','random'):
        edge.kb.edges={k:copy.copy(e) for k,e in native_edges.items()}
        set_weights(edge.kb,mode,weights)
        weight_maps[mode]=edge.kb.edges
    edge.kb.edges=native_edges
    lattice.prepare();progress('loaded_baseline',nodes=lattice.node_count,edge_nodes=len(edge.kb.nodes))
    names=['tool','trie','lattice_shrinkage','lattice_loso','lattice_max_cardinality','edge_native','edge_uniform','edge_shared','edge_shuffled','edge_random','edge_no_weights']
    rows={n:[] for n in names};cache={};evidence={};t0=time.monotonic()
    with gzip.open(out/'predictions.jsonl.gz','wt',encoding='utf8') as stream:
        for i,c in enumerate(calls):
            key=fingerprint([c['repo'],c['tool'],c['command'],c['clauses'],c['parse_reason']])
            if key not in cache:
                result={};ev={};clauses=c['clauses'];reason=c['parse_reason']
                direct=runtime.predict_load_samples(ToolCallQuery(c['repo'],c['tool'],c['command'],1e12))
                result['tool']=estimate_row(summarize('duration_ms',EDGES,'runtime',direct.get('duration_ms',{}).get('values',()),context=direct.get('duration_ms',{}).get('context',())))
                ev['tool']=direct.get('duration_ms',{})
                tr=trie.predict_load_samples(c['repo'],clauses,1e12) if not reason else ()
                ev['trie']=tr;result['trie']=estimate_row(compose('trie',tr,edges,reason=reason,clauses=clauses).targets['duration_ms'])
                for algorithm in ('shrinkage','loso','max_cardinality'):
                    name='lattice_'+algorithm
                    raw=lattice.predict_load_samples(c['repo'],clauses,1e12,algorithm=algorithm) if not reason else ()
                    ev[name]=raw;result[name]=estimate_row(compose('lattice',raw,edges,reason=reason,clauses=clauses).targets['duration_ms'])
                for mode in ('native','uniform','shared','shuffled','random','no_weights'):
                    edge.kb.edges=weight_maps[mode]
                    raw,diag=edge_evidence(edge.kb,clauses,c['repo'],mode) if not reason else ([],[])
                    name='edge_'+mode;ev[name]={'clauses':raw,'decisions':diag}
                    result[name]=estimate_row(compose('edge_kappa',raw,edges,reason=reason,clauses=clauses).targets['duration_ms'])
                cache[key]=result;evidence[key]=ev
            for name,p in cache[key].items():
                row={k:c[k] for k in ('id','task','repo','repo_group','partition','tool','command','actual_ms','actual_bucket','trace')}
                row.update(model=name,evidence_key=key,**p)
                row['original_partition']='train' if c['task'] in original_split['train'] else 'test'
                stream.write(json.dumps(row,ensure_ascii=False)+'\n');rows[name].append(row)
            if i%250==0:progress('frozen',calls=i,unique_queries=len(cache),elapsed_s=time.monotonic()-t0)
    edge.kb.edges=native_edges
    state_after={k:fingerprint(v.to_snapshot() if k=='edge' else v.to_json_obj()) for k,v in models.items()}
    after={p.name:digest(p) for p in baseline.glob('*.json')}
    write(out/'freeze-verification.json',{'files_before':hashes,'files_after':after,'states_before':state_before,'states_after':state_after,
        'unchanged_files':hashes==after,'unchanged_states':state_before==state_after,'test_updates':0})
    assert hashes==after and state_before==state_after
    write(out/'evidence.json.gz',evidence)
    summary={}
    for scope in ('all','original_test','dev'):
        selected={n:[r for r in rr if scope=='all' or scope=='original_test' and r['original_partition']=='test' or scope=='dev' and r['partition']=='dev'] for n,rr in rows.items()}
        common=set.intersection(*({r['id'] for r in rr if r['p50'] is not None} for rr in selected.values()))
        summary[scope]={'full':{n:metrics(rr) for n,rr in selected.items()},'common_n':len(common),
                        'common':{n:metrics([r for r in rr if r['id'] in common]) for n,rr in selected.items()}}
    # Do not inspect full-data errors until optimization choices have been locked.
    write(out/'baseline-metrics.json',summary)
    write(out/'variant-manifest.json',{'evaluated':names,'configuration':{'lattice_max_optional_features':lattice.max_optional_features,
          'edge':edge.kb.to_snapshot()['config'],'bucket_edges_ms':EDGES},
          'source':str(baseline),'other_interfaces':{
          'lattice_predict_clauses':'legacy point interface: same 3 selectors, positive-duration-only; separately audited',
          'offline.edge_kappa_legacy':'original shrinkage bucket wrapper; not an independent trained state',
          'offline.lattice_oracle':'uses actual test label for selection; excluded from deployable ranking',
          'edge_shared_child_training':'independent training experiment',
          'subset_coverage_false':'legacy compatibility retained in Lattice snapshot loader; evaluated separately'},
          'weight_modes':{'uniform':'k0 / number of all parents','shared':'same learned total, equal across each child parents',
          'shuffled':'per-child permutation, seed42','random':'log-uniform then normalized to k0, seed42',
          'no_weights':'identical candidate/evidence union, each unique real duration event weight=1'}})

def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['prepare','frozen']);p.add_argument('--dataset',type=Path)
    p.add_argument('--out',type=Path,default=HERE/'results');p.add_argument('--baseline',type=Path,default=Path('baseline'))
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if a.phase=='prepare':prepare(a.dataset,a.out)
    else:frozen(a.out,a.baseline)

if __name__=='__main__':main()
