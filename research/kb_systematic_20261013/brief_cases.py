import json
from research.kb_systematic_20261013.study import HERE,read
for model in ('lattice_shrinkage','edge_native'):
    print(model)
    for r in read(HERE/'results'/(model+'-worst30.json.gz')):
        cls=[]
        for c in r['clauses']:
            lat=c['lattice'];ed=c['edge']
            cls.append({'argv':c['argv'][:5],'parse':c['parsed']['status'],'diagnostics':c['parsed']['diagnostics'],
                        'lattice_n':len(lat['selected_values_ms']),'lattice_exact':lat['exact'],'edge_n':ed.get('observation_count'),
                        'edge_exact':ed.get('exact_match'),'parents':[(e['difference_count'],e['kappa'],e['dropped_features']) for e in ed.get('evidence',[])]})
        print(json.dumps({'rank':r['rank'],'id':r['id'],'actual_s':round(r['actual_ms']/1000,4),'pred_s':round(r['p50']/1000,4),
                          'command':r['command'][:240],'class':r['attribution']['primary'],'clauses':cls},ensure_ascii=False))
