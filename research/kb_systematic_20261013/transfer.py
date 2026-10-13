"""Experimental source-trained edge-effect sharing and task-isolated calibration."""
from collections import defaultdict
from dataclasses import replace
import argparse
import hashlib
import math
from pathlib import Path
import statistics
import time
from edge_kappa_kb import EdgeKappaKB
from edge_kappa_kb.kb import SMOOTHING
from research.kb_systematic_20261013.study import HERE,read,write,progress,metrics
from research.kb_systematic_20261013.optimization import feature_key,evaluate,event_obj,fit_model

def pooled_effects(kb):
    rows=defaultdict(list)
    for (parent,child),e in kb.edges.items():
        if not e.update_count:continue
        tool=next(f for f in kb.graph.signatures[child] if f.startswith('tool='))
        dropped=kb.graph.signatures[child]-kb.graph.signatures[parent]
        if not dropped:continue
        ratio=e.log_kappa-math.log(kb.k0/len(kb.graph.parents[child]))
        for f in dropped:rows[(tool,feature_key(f))].append((ratio,e.update_count))
    return {k:(sum(v*n for v,n in vs)/sum(n for v,n in vs),sum(n for v,n in vs),len(vs)) for k,vs in rows.items()}

class TransferKB(EdgeKappaKB):
    def predict(self,query):
        pred=super().predict(query)
        if pred.exact_match or not pred.evidence:return pred
        scores=[]
        for e in pred.evidence:
            logs=[]
            for f in e.dropped_features:
                if self.transfer_kind=='semantic':logs.append(-math.log(self.semantic.get(feature_key(f),1.)))
                else:
                    v,n,_=self.effects.get(('tool='+query.tool,feature_key(f)),(0.,0,0))
                    logs.append(v*n/(n+self.shrink) if n+self.shrink else 0.)
            scores.append(math.exp(max(-3.,min(3.,statistics.mean(logs) if logs else 0.))))
        total=sum(e.kappa for e in pred.evidence);denominator=pred.observation_count+SMOOTHING+total
        evidence=tuple(replace(e,kappa=total*s/sum(scores),mixture_share=total*s/sum(scores)/denominator) for e,s in zip(pred.evidence,scores))
        # Temporary node has no own coverage; retain exactly the same smoothing.
        probs=tuple((SMOOTHING/self.bucket_count+sum(e.kappa*e.distribution[b] for e in evidence))/denominator for b in range(self.bucket_count))
        return replace(pred,evidence=evidence,probabilities=probs,bucket=max(range(self.bucket_count),key=lambda b:(probs[b],-b)))

def run_transfer(out):
    kb=EdgeKappaKB.from_snapshot(read(out/'optimization/native/model.json.gz'));kb.freeze()
    effects=pooled_effects(kb)
    write(out/'parameter-effects.json',{'effects':[{'tool':t,'feature_key':f,'log_ratio':v,'gradient_updates':n,'edges':k} for (t,f),(v,n,k) in sorted(effects.items())],
        'meaning':'observational pooled log-kappa deviations, not causal parameter runtime importance'})
    calls=read(out/'calls.json.gz');dev=[c for c in calls if c['partition']=='dev']
    configs=[('native',None,0),('pooled','pooled',0),('pooled_shrink100','pooled',100),('semantic_transfer','semantic',0)]
    scores={};preds={}
    for name,kind,shrink in configs:
        kb.__class__=EdgeKappaKB if kind is None else TransferKB
        kb.effects=effects;kb.shrink=shrink;kb.transfer_kind=kind
        kb.semantic=read(out/'semantic-prior.json')['weights']
        rr=evaluate(kb,dev);preds[name]=rr;write(out/'transfer'/f'{name}-dev.json.gz',rr);scores[name]=metrics(rr)
    best=min(scores,key=lambda n:(scores[n]['log_mae'] if scores[n]['log_mae'] is not None else math.inf,n!='native'))
    write(out/'transfer-lock.json',{'configs':configs,'dev':scores,'selected':best,'locked_unix':time.time(),'test_read_before_lock':False})
    test=[c for c in calls if c['partition']=='test'];test_scores={}
    for name,kind,shrink in configs:
        kb.__class__=EdgeKappaKB if kind is None else TransferKB
        kb.effects=effects;kb.shrink=shrink;kb.transfer_kind=kind
        rr=evaluate(kb,test);write(out/'transfer'/f'{name}-test.json.gz',rr);test_scores[name]=metrics(rr)
        progress('transfer_test',name=name)
    write(out/'transfer-test.json',{'selected':best,'test':test_scores})

def cold(out):
    rows=read(out/'events.json.gz');source=[event_obj(r) for r in rows if r['partition']=='train']
    target=[r for r in rows if r['partition']=='test'];tasks=sorted({r['event']['outcome']['task_id'] for r in target},key=lambda t:hashlib.sha256(('calibration20261013:'+t).encode()).hexdigest())
    calibration_tasks=set(tasks[:10]);pool=sorted([r for r in target if r['event']['outcome']['task_id'] in calibration_tasks],
        key=lambda r:(tasks.index(r['event']['outcome']['task_id']),r['event']['outcome']['start_time'],r['event']['outcome']['event_id']))
    calls=[c for c in read(out/'calls.json.gz') if c['partition']=='test' and c['task'] not in calibration_tasks]
    write(out/'calibration-protocol.json',{'calibration_tasks':sorted(calibration_tasks),'test_tasks':sorted({c['task'] for c in calls}),
         'feedback_sizes':[0,5,20,100],'feedback_available':len(pool),'test_calls':len(calls),'fixed_test_ids':[c['id'] for c in calls],
         'selection':'fixed before curve evaluation; same first source-independent task order for all methods'})
    semantics=read(out/'semantic-prior.json')['weights'];summary=[]
    for n in (0,5,20,100):
        feedback=[event_obj(r) for r in pool[:n]]
        for name,config in [('native',{'learning_rate':.1}),('uniform',{'learn_weights':False}),('semantic',{'learning_rate':.1})]:
            dest=out/'calibration'/f'{name}-{n}.json.gz'
            if dest.exists():rr=read(dest)
            else:
                progress('calibration_fit',name=name,n=n)
                kb=fit_model(source+feedback,config,semantics if name=='semantic' else None)
                rr=evaluate(kb,calls);write(dest,rr)
            summary.append({'name':name,'feedback':n,'metrics':metrics(rr)})
            progress('calibration_done',name=name,n=n)
    write(out/'calibration-summary.json',summary)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['transfer','cold']);p.add_argument('--out',type=Path,default=HERE/'results');a=p.parse_args()
    run_transfer(a.out) if a.phase=='transfer' else cold(a.out)
