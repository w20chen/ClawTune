"""Model mechanisms, curve common sets, independent syntax checks and costs."""
from collections import Counter,defaultdict
import json
import math
from pathlib import Path
import statistics
from research.kb_systematic_20261013.study import HERE,read,write,metrics
from research.kb_systematic_20261013.analysis import paired

def main():
    out=HERE/'results';base=read(Path('baseline/edge-kappa-kb.json'))
    edges=base.get('edges',[])
    write(out/'edge-snapshot-statistics.json',{'config':base['config'],'nodes':len(base['nodes']),'edges':len(edges),
        'updated_edges':sum(e['update_count']>0 for e in edges),'update_count':sum(e['update_count'] for e in edges),
        'kappa_min':min(math.exp(e['log_kappa']) for e in edges),'kappa_max':max(math.exp(e['log_kappa']) for e in edges),
        'different_log_weights':len({e['log_kappa'] for e in edges})})
    curve={}
    for p in (out/'calibration').glob('*.json.gz'):curve[p.name.removesuffix('.json.gz')]=read(p)
    common=set.intersection(*({r['id'] for r in rr if r['p50'] is not None} for rr in curve.values()))
    curve_ci={}
    for n in (0,5,20,100):
        for name in ('uniform','semantic'):curve_ci[f'native_vs_{name}_{n}']=paired(curve[f'native-{n}'],curve[f'{name}-{n}'])
        curve_ci[f'native_0_vs_{n}']=paired(curve['native-0'],curve[f'native-{n}'])
    write(out/'calibration-common.json',{'common_n':len(common),'metrics':{n:metrics([r for r in rr if r['id'] in common]) for n,rr in curve.items()},'paired':curve_ci})
    # Check actual dataset examples against independently specified grammar facts.
    # These checks do not use LLM-generated fixtures as semantic ground truth.
    checks=[]
    for partition in ('dev','test'):
        changes=read(out/'llm'/f'{partition}-parse-changes.json.gz')
        for r in changes:
            argv=r['argv'];entry=Path(argv[0]).name;parsed=r['parsed_after'];expected=None
            if entry=='apt-get' and len(argv)>1 and argv[1] in ('update','install','remove','purge','clean','autoremove'):
                expected=[argv[1]]
            elif entry=='apt-cache' and len(argv)>1 and argv[1] in ('policy','search','show','depends','showpkg'):
                expected=[argv[1]]
            elif entry=='sort':expected=[]
            elif len(argv)>2 and argv[1:3]==['-m','ensurepip']:expected=['ensurepip']
            if expected is not None:
                checks.append({'id':r['id'],'clause_index':r['clause_index'],'argv':argv,'expected_target':expected,
                               'actual_target':parsed['target'],'target_pass':expected==parsed['target'],'status':parsed['status'],
                               'options':[o for l in parsed['layers'] for o in l['options']],
                               'positionals':[v for l in parsed['layers'] for v in l['args']]})
    write(out/'llm/independent-syntax-checks.json',{'checked':len(checks),'target_correct':sum(r['target_pass'] for r in checks),
        'scope':'execution mode/subcommand target on real held-out commands matching pre-specified unambiguous forms. Not full token-level semantic accuracy; options/values retained for inspection.',
        'evidence':'isolated help evidence in saved provider requests; parser schema semantics; no runtime labels','rows':checks})
    accounting=read(out/'llm/accounting.json');t=accounting['tokens']
    cost=(.02*t.get('prompt_cache_hit_tokens',0)+1*t.get('prompt_cache_miss_tokens',0)+4*t.get('completion_tokens',0))/1e6
    accounting.update(estimated_cny_offpeak_recorded=cost,estimated_cny_peak_recorded=cost*2,
        price_source='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
        rates_cny_per_million={'offpeak_hit':.02,'offpeak_miss':1,'offpeak_output':4},
        price_observed_date='2026-10-13',excludes='unrecorded interrupted request billing')
    write(out/'llm/accounting.json',accounting)

if __name__=='__main__':main()
