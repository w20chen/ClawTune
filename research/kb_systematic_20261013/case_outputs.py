import json
from research.kb_systematic_20261013.study import HERE,read
seen=set()
for model in ('lattice_shrinkage','edge_native'):
    print(model)
    for r in read(HERE/'results'/(model+'-worst30.json.gz')):
        if r['id'] in seen:continue
        seen.add(r['id'])
        print(json.dumps({'rank':r['rank'],'id':r['id'],'actual_s':round(r['actual_ms']/1000,3),'pred_s':round(r['p50']/1000,3),
            'command':r['command'][:100],'exit':r['raw_exit_code'],'output':r['output_tail'][-550:],
            'measured':[(x['argv'][:4],x['latency_ms']) for x in r['measured_clauses']]},ensure_ascii=False))
