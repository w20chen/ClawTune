"""Compatibility/legacy point interfaces on their actual clause-level target."""
from bisect import bisect_right
from dataclasses import asdict
import gzip
import json
from pathlib import Path
import shlex
from research.kb_systematic_20261013.study import HERE,EDGES,read,write,progress,metrics
from research.kb_systematic_20261013.optimization import event_obj

def main():
    from tool_time.lattice_kb import LatticeTimeKB
    from command_parser.models import CommandFeatures
    from command_parser import default_service,ParseContext
    out=HERE/'results';base=read(Path('baseline/clause-lattice-time-kb.json'))
    events=read(out/'events.json.gz');summary={}
    for compatibility in (False,True):
        kb=LatticeTimeKB.from_json_obj(base)
        if compatibility:
            kb=LatticeTimeKB.fit(kb._observations,subset_coverage=False,max_optional_features=6)
        kb.freeze();kb.prepare();name='legacy_subset' if compatibility else 'current_point'
        cache={};rows=[]
        for i,r in enumerate(events):
            e=event_obj(r);q=e.query;repo=next((json.loads(f.split(':',1)[1])[1] for f in q.features if f.startswith('context:["repo",')),None)
            key=(repo,e.source_command)
            if key not in cache:
                features=default_service().argv(shlex.split(e.source_command),ParseContext(repo=repo))
                cache[key]=kb._predict_clause(repo,tuple(shlex.split(e.source_command)),features)
            for pred in cache[key]:
                rows.append({'id':e.outcome.event_id,'task':e.outcome.task_id,'repo':repo,'model':name+'_'+pred.algorithm,
                    'actual_ms':e.outcome.duration_ms,'actual_bucket':bisect_right(EDGES,e.outcome.duration_ms),
                    'p50':pred.prediction_ms,'predicted_bucket':bisect_right(EDGES,pred.prediction_ms) if pred.prediction_ms is not None else None,
                    'decision':asdict(pred)})
            if i%1000==0:progress('interfaces',name=name,events=i)
        write(out/'interfaces'/f'{name}-predictions.json.gz',rows)
        for alg in ('shrinkage','loso','max_cardinality'):
            summary[name+'_'+alg]=metrics([r for r in rows if r['model']==name+'_'+alg])
    write(out/'interfaces/summary.json',{'target':'individual recorded clause duration, not tool-call duration',
        'legacy_subset':'independent reconstruction with subset_coverage=False; original current KB unchanged',
        'models':summary})

if __name__=='__main__':main()
