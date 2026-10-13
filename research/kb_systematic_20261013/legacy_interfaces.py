"""Audit the remaining actual legacy APIs without conflating their targets."""
from collections import defaultdict
from dataclasses import asdict
from bisect import bisect_right
import gzip
import json
from pathlib import Path
from research.kb_systematic_20261013.study import HERE, EDGES, read, write, metrics, fingerprint, progress

def main():
    from tool_resource.runtime_kb import RuntimeToolResourceKB, ClauseResourceKB, ToolCallQuery, LatencyBuckets, _target_values
    from offline.runner import load_task
    out=HERE/'results';base=Path('baseline');calls=read(out/'calls.json.gz')
    trie=ClauseResourceKB.from_json_obj(read(base/'clause-resource-kb.json'));trie.freeze()
    tool=RuntimeToolResourceKB.from_json_obj(read(base/'runtime-tool-resource-kb.json'));tool.freeze()
    before=[fingerprint(trie.to_json_obj()),fingerprint(tool.to_json_obj())]
    original=read(base/'original-split.json');bucket=LatencyBuckets(EDGES);cache={};rows=[]
    for i,c in enumerate(calls):
        key=(c['repo'],c['command'])
        if key not in cache:
            point=None;pred=None;decision=None
            if c['command']:
                decision=trie.predict_command_latency_bucket(c['repo'],c['command'],1e12,bucket)
                if decision.prediction is not None:
                    pred=decision.prediction.bucket_id
                    point=decision.composed_total_ms
                    if point is None and len(decision.clause_predictions)==1:
                        cl=decision.clause_predictions[0]
                        point=trie._clause_median_latency_ms(c['repo'],cl.bin,cl.argv)
            cache[key]={'p50':point,'predicted_bucket':pred,'decision':asdict(decision) if decision else None}
        rows.append({**c,**cache[key],'model':'trie_legacy_median_composition',
            'original_partition':'train' if c['task'] in original['train'] else 'test'})
        if i%3000==0:progress('legacy-trie',calls=i)
    write(out/'interfaces/trie-legacy-call-predictions.json.gz',rows)
    groups=defaultdict(list)
    with gzip.open(out/'predictions.jsonl.gz','rt') as f:
        for line in f:
            r=json.loads(line);groups[r['model']].append(r)
    groups['trie_legacy_median_composition']=rows
    summary={}
    for scope in ('all','original_test'):
        selected={n:[r for r in rr if scope=='all' or r['original_partition']=='test'] for n,rr in groups.items()}
        common=set.intersection(*({r['id'] for r in rr if r['p50'] is not None} for rr in selected.values()))
        summary[scope]={'common_n':len(common),'all':{n:metrics(rr) for n,rr in selected.items()},
            'common':{n:metrics([r for r in rr if r['id'] in common]) for n,rr in selected.items()}}
    write(out/'interfaces/call-compatibility-summary.json',summary)
    # Legacy Tool query predicts a conditional p90 of the entire tool interval,
    # unlike the retained workload p50 targeted by call_load.v2.
    inv=read(out/'inventory.json');rows=[];cache={}
    for key,task in sorted(inv['tasks'].items()):
        loaded=load_task(Path(inv['dataset']),task,'MiB')
        for i,c in enumerate(loaded.calls):
            actual=_target_values(c).get('latency_ms')
            if actual is None:continue
            query_key=(c.repo,c.tool_name,c.command)
            if query_key not in cache:
                cache[query_key]=asdict(tool.query(ToolCallQuery(c.repo,c.tool_name,c.command,1e12))['latency_ms'])
            decision=cache[query_key];point=decision['conditional_p90']
            rows.append({'id':key+':'+str(i),'task':key,'repo':c.repo,'command':c.command,'tool':c.tool_name,
                'actual_ms':actual,'actual_bucket':bisect_right(EDGES,actual),'p50':point,
                'point_statistic':'conditional_p90; p50 field is generic metric adapter only',
                'predicted_bucket':bisect_right(EDGES,point) if point is not None else None,
                'decision':decision,'original_partition':'train' if key in original['train'] else 'test'})
    write(out/'interfaces/tool-legacy-p90-predictions.json.gz',rows)
    write(out/'interfaces/tool-legacy-p90-summary.json',{'target':'whole-tool interval, conditional p90; not retained workload median',
        'all':metrics(rows),'original_test':metrics([r for r in rows if r['original_partition']=='test'])})
    after=[fingerprint(trie.to_json_obj()),fingerprint(tool.to_json_obj())]
    write(out/'interfaces/freeze.json',{'before':before,'after':after,'unchanged':before==after});assert before==after

if __name__=='__main__':main()
