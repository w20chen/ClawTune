"""Repository-disjoint experiments; tuning and semantic prompts never see test labels."""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import asdict, replace
import argparse
import json
import math
from pathlib import Path
import statistics
import time

from research.kb_systematic_20261013.study import HERE, EDGES, read, write, progress, metrics, estimate_row, edge_evidence, fingerprint

def feature_key(f):
    if ':' not in f:return f.split('=')[0]
    kind,body=f.split(':',1)
    try:args=json.loads(body)
    except ValueError:return kind
    if kind in ('opt','flag','repeat','count'):return kind+':'+str(args[0])+':'+str(args[1])
    if kind=='context':return 'context:'+str(args[0])
    if kind=='arg':return 'arg:'+str(args[0])+':'+str(args[1])
    if kind=='mode':return f
    return kind+':'+str(args[0] if args else '')

def events_prepare(out):
    from offline.edge_kappa_trace import read_v5_events
    inv=read(out/'inventory.json');split=read(out/'optimization-split.json');events=[];excluded=Counter()
    for i,(key,task) in enumerate(sorted(inv['tasks'].items())):
        ev,ex=read_v5_events(Path(inv['dataset']),task,key);excluded.update(ex)
        events.extend({'partition':split['tasks'][key],'repo_group':task['group'],'event':asdict(e)} for e in ev)
        if i%100==0:progress('trace-events',tasks=i,events=len(events))
    write(out/'events.json.gz',events)
    write(out/'event-inventory.json',{'excluded':dict(excluded),'by_partition':dict(Counter(r['partition'] for r in events)),
        'by_repository':dict(Counter(r['repo_group'] for r in events)),
        'protocol':'existing offline.edge_kappa_trace.read_v5_events; exact real clause durations and timestamps'})

def event_obj(row):
    from edge_kappa_kb import FeatureQuery,TimeOutcome,TrainingEvent
    e=row['event'];return TrainingEvent(FeatureQuery(**e['query']),TimeOutcome(**e['outcome']),e['source_command'])

def isolate(out):
    """Metadata-only split repair, before hyperparameter fitting or test inspection."""
    original=read(Path('baseline/original-split.json'));split=read(out/'optimization-split.json')
    if (out/'selection-lock.json').exists():raise RuntimeError('cannot change a locked split')
    if not (out/'initial-repository-split.json').exists():write(out/'initial-repository-split.json',split)
    calls=read(out/'calls.json.gz');events=read(out/'events.json.gz');obs=read(out/'observations.json.gz')
    def partition(task,repo):
        if repo=='tobymao/sqlglot':return 'dev' if task in original['test'] else 'train'
        return 'test' if repo=='PennyLaneAI/pennylane' else 'auxiliary'
    for c in calls:c['partition']=partition(c['task'],c['repo_group'])
    for e in events:e['partition']=partition(e['event']['outcome']['task_id'],e['repo_group'])
    for o in obs:o['partition']=partition(o['task'],o['repo_group'])
    split={'protocol':'source SQLGlot tasks use existing seed42 assignment for train/dev; all PennyLane tasks are target test; other repos auxiliary',
           'reason':'Only two repositories have eligible timestamped clause labels; original random dev has none',
           'tasks':{c['task']:c['partition'] for c in calls},'changed_before_tuning':True,'assigned_unix':time.time()}
    write(out/'optimization-split.json',split);write(out/'calls.json.gz',calls);write(out/'events.json.gz',events);write(out/'observations.json.gz',obs)
    write(out/'split-counts.json',{'calls':dict(Counter(c['partition'] for c in calls)),
        'events':dict(Counter(e['partition'] for e in events)),
        'tasks':dict(Counter(split['tasks'].values()))})

def semantic(out):
    from research.kb_systematic_20261013.provider import Provider
    training=[r for r in read(out/'events.json.gz') if r['partition']=='train']
    counts=Counter(feature_key(f) for r in training for f in r['event']['query']['features'])
    features=[k for k,v in counts.most_common(120)]
    provider=Provider(out/'llm/semantic');provider.models()
    messages=[{'role':'system','content':'You initialize relative importance of CLI feature dimensions for execution workload. No times, no performance labels, and no guessed runtimes. Return JSON with weights mapping each provided key to a number in [0.25,4] and rationale mapping keys to short explanations. 1 means neutral. Larger means removing this feature makes historical runtime evidence less transferable. Distinguish syntax-only formatting from input scope, execution mode and workload; acknowledge uncertainty. Keys include parameter identity, not observed effect estimates.'},
              {'role':'user','content':json.dumps({'feature_keys':features})}]
    answer=provider({'messages':messages,'response_format':{'type':'json_object'},'temperature':0,'max_tokens':8192})
    data=json.loads(answer);weights=data['weights']
    assert all(isinstance(v,(int,float)) and math.isfinite(v) and .25<=v<=4 for v in weights.values())
    write(out/'semantic-prior.json',{'weights':{k:weights.get(k,1.) for k in features},'rationale':data.get('rationale'),
                                    'prompt_version':'workload-importance-v1','label_access':'none; training feature keys only'})

def fit_model(events,config,semantic_weights=None):
    from edge_kappa_kb import EdgeKappaKB
    class Initialized(EdgeKappaKB):
        def _init_edges(self):
            super()._init_edges()
            if semantic_weights:
                for child,parents in self.graph.parents.items():
                    if not parents:continue
                    vals=[]
                    for p in parents:
                        dropped=self.graph.signatures[child]-self.graph.signatures[p]
                        importance=statistics.mean(semantic_weights.get(feature_key(f),1.) for f in dropped) if dropped else 1.
                        vals.append(1./importance)
                    for p,v in zip(parents,vals):self.edges[(p,child)].log_kappa=math.log(self.k0*v/sum(vals))
    kb=Initialized.fit(events,dict(config,max_optional_features=6,bucket_edges_ms=EDGES));kb.freeze();return kb

def evaluate(kb,calls):
    from clawtune_sidecar.predictors.call_load import compose
    from clawtune_sidecar.prediction_config import load_bucket_edges
    cache={};rows=[];edges=load_bucket_edges(EDGES)
    for c in calls:
        key=fingerprint([c['repo'],c['clauses'],c['parse_reason']])
        if key not in cache:
            raw,decisions=edge_evidence(kb,c['clauses'],c['repo']) if not c['parse_reason'] else ([],[])
            estimate=compose('edge_kappa',raw,edges,reason=c['parse_reason'],clauses=c['clauses']).targets['duration_ms']
            cache[key]={'prediction':estimate_row(estimate),'decisions':decisions}
        rows.append({**{k:c[k] for k in ('id','task','repo','repo_group','actual_ms','actual_bucket')},**cache[key]['prediction'],
                     'decisions':cache[key]['decisions']})
    return rows

def train(out):
    rows=read(out/'events.json.gz');events=[event_obj(r) for r in rows if r['partition']=='train']
    calls=[c for c in read(out/'calls.json.gz') if c['partition']=='dev']
    semantics=read(out/'semantic-prior.json')['weights'] if (out/'semantic-prior.json').exists() else None
    configs=[('native',{'learning_rate':.1}),('uniform',{'learn_weights':False}),
             ('lr003',{'learning_rate':.03}),('lr03',{'learning_rate':.3}),
             ('shared_child',{'shared_child_weight':True}),('k03',{'k0':.3}),('k3',{'k0':3.})]
    if semantics:configs += [('semantic',{'learning_rate':.1}),('semantic_fixed',{'learn_weights':False})]
    write(out/'optimization-preregistered.json',{'configs':configs,'selection':'minimum dev log1p(seconds) MAE on common available calls; tie native',
          'max_optional_features':6,'test_access_before_lock':False,'events':len(events),'dev_calls':len(calls)})
    all_rows={};summary={}
    for name,config in configs:
        dest=out/'optimization'/name
        if (dest/'dev.json.gz').exists():
            all_rows[name]=read(dest/'dev.json.gz');summary[name]=read(dest/'fit.json');continue
        progress('fit',name=name,events=len(events));start=time.monotonic()
        kb=fit_model(events,config,semantics if name.startswith('semantic') else None)
        write(dest/'model.json.gz',kb.to_snapshot());rr=evaluate(kb,calls);write(dest/'dev.json.gz',rr);all_rows[name]=rr
        summary[name]={'config':config,'fit_eval_s':time.monotonic()-start,'metrics':metrics(rr),
                       'nodes':len(kb.nodes),'edges':len(kb.edges),'updates':sum(e.update_count for e in kb.edges.values())}
        write(dest/'fit.json',summary[name]);progress('fit_done',name=name,**summary[name])
    common=set.intersection(*({r['id'] for r in rr if r['p50'] is not None} for rr in all_rows.values()))
    scores={n:metrics([r for r in rr if r['id'] in common]) for n,rr in all_rows.items()}
    eligible=[n for n in scores if scores[n]['log_mae'] is not None]
    best=min(eligible,key=lambda n:(scores[n]['log_mae'],n!='native')) if eligible else 'native'
    write(out/'selection-lock.json',{'best':best,'common_n':len(common),'dev_common':scores,'dev':summary,
                                   'locked_unix':time.time(),'heldout_viewed':False,
                                   'training_repositories':sorted({r['repo_group'] for r in rows if r['partition']=='train'})})

def test(out):
    from edge_kappa_kb import EdgeKappaKB
    lock=read(out/'selection-lock.json');calls=[c for c in read(out/'calls.json.gz') if c['partition']=='test']
    summaries={};preds={}
    for name in lock['dev']:
        kb=EdgeKappaKB.from_snapshot(read(out/'optimization'/name/'model.json.gz'));kb.freeze()
        rr=evaluate(kb,calls);preds[name]=rr;write(out/'optimization'/name/'test.json.gz',rr);summaries[name]=metrics(rr)
        progress('heldout_done',name=name,n=len(rr))
    common=set.intersection(*({r['id'] for r in rr if r['p50'] is not None} for rr in preds.values()))
    write(out/'optimization-test.json',{'selected':lock['best'],'all':summaries,'common_n':len(common),
         'common':{n:metrics([r for r in rr if r['id'] in common]) for n,rr in preds.items()}})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['events','isolate','semantic','train','test']);p.add_argument('--out',type=Path,default=HERE/'results');a=p.parse_args()
    {'events':events_prepare,'isolate':isolate,'semantic':semantic,'train':train,'test':test}[a.phase](a.out)
