"""Capture actual decisions and counterexamples after choices are locked."""
from collections import Counter,defaultdict
from dataclasses import asdict
import argparse
import gzip
import json
import math
from pathlib import Path
import statistics

from research.kb_systematic_20261013.study import HERE,EDGES,read,write,progress,metrics

def rows(out):
    with gzip.open(out/'predictions.jsonl.gz','rt',encoding='utf8') as f:
        return [json.loads(l) for l in f]

def summary_stats(values):
    vs=sorted(values)
    return {'n':len(vs),'min':vs[0] if vs else None,'median':statistics.median(vs) if vs else None,
            'max':vs[-1] if vs else None,'p90':vs[math.ceil(.9*len(vs))-1] if vs else None}

def ablations(out):
    rr=rows(out);by=defaultdict(dict)
    for r in rr:by[r['model']][r['id']]=r
    ev=read(out/'evidence.json.gz');results={}
    for name in ('edge_uniform','edge_shared','edge_shuffled','edge_random','edge_no_weights'):
        counts=Counter();paired=[]
        for ident,a in by['edge_native'].items():
            b=by[name][ident];ad=ev[a['evidence_key']]['edge_native']['decisions'];bd=ev[a['evidence_key']][name]['decisions']
            if a['p50'] is None:ad=[]
            if b['p50'] is None:bd=[]
            counts['calls']+=1
            counts['node_changed']+=int([x['node_id'] for x in ad]!=[x['node_id'] for x in bd])
            counts['parent_set_changed']+=int([[x['parent_id'] for x in y['evidence']] for y in ad]!=[[x['parent_id'] for x in y['evidence']] for y in bd])
            counts['weights_changed']+=int([[x['kappa'] for x in y['evidence']] for y in ad]!=[[x['kappa'] for x in y['evidence']] for y in bd])
            counts['point_changed']+=int(a['p50']!=b['p50']);counts['bucket_changed']+=int(a['predicted_bucket']!=b['predicted_bucket'])
            counts['probabilities_changed']+=int(a['buckets']['probabilities']!=b['buckets']['probabilities'])
            if a['p50'] is not None and b['p50'] is not None:paired.append((a,b))
        results[name]={'decisions':dict(counts),'native':metrics([a for a,b in paired]),'candidate':metrics([b for a,b in paired])}
    signatures=Counter();contributions=[]
    for r in by['edge_native'].values():
        if r['p50'] is None:continue
        for d in ev[r['evidence_key']]['edge_native']['decisions']:
            label='exact' if d['exact_match'] else 'temporary';signatures[label]+=1
            signatures[label+'_no_parents']+=int(not d['evidence'])
            signatures[label+'_own_evidence']+=int(d['observation_count']>0)
            contributions.append(sum(p['mixture_share'] for p in d['evidence']))
    write(out/'weight-ablation.json',{'comparisons':results,'query_paths':dict(signatures),'parent_mixture_share':summary_stats(contributions)})

def worst(out,baseline):
    if not (out/'selection-lock.json').exists():raise RuntimeError('no full-data inspection before selection lock')
    from tool_time.lattice_kb import LatticeTimeKB
    from tool_time.query import query_nodes
    from command_parser.service import clause_features,observation_features
    from command_parser import default_service,ParseContext
    from tool_time._lattice_vendor.risk import compute_candidate_risk
    from tool_resource.runtime_kb import is_pipeline_dependent_consumer
    from tool_time import lattice_kb as config
    rr=rows(out);calls={c['id']:c for c in read(out/'calls.json.gz')};ev=read(out/'evidence.json.gz')
    lattice=LatticeTimeKB.from_json_obj(read(baseline/'clause-lattice-time-kb.json'));lattice.freeze();lattice.prepare()
    state=lattice._resource_states['load:duration_ms']
    train=[(o,observation_features(o)) for o in lattice._observations if o.latency_ms is not None and not o.in_loop and not o.in_subst and not is_pipeline_dependent_consumer(o)]
    dataset=Path(read(out/'inventory.json')['dataset']);all_details={};source_cache={}
    for model in ('lattice_shrinkage','edge_native'):
        selected=sorted([r for r in rr if r['model']==model and r['p50'] is not None],
                        key=lambda r:(-abs(math.log1p(r['p50']/1000)-math.log1p(r['actual_ms']/1000)),r['id']))[:30]
        result=[]
        for rank,r in enumerate(selected,1):
            c=calls[r['id']];e=ev[r['evidence_key']];details=[]
            for ci,cl in enumerate(cl for cl in c['clauses'] if not is_pipeline_dependent_consumer(cl)):
                cf=clause_features(cl,c['repo']);parsed=default_service().parser.parse(cl.get('syntax_argv',cl['argv']),ParseContext(repo=c['repo']),word_intents=cl.get('word_intents'))
                candidates=query_nodes(state.nodes,cf,global_log_std=state.global_log_std,index=state.query_index)
                candidate_rows=[]
                for fs,n in candidates.items():
                    risk=compute_candidate_risk(n,cf.features,global_log_var=state.global_log_var,global_log_std=state.global_log_std,
                                                risk_method='shrinkage',context_sample_alpha=config._CONTEXT_SAMPLE_ALPHA,gamma=0)
                    candidate_rows.append({'features':sorted(fs),'n':n.count,'own_n':len(n.own_durations),
                                           'stats_ms':summary_stats([v*1000 for v in n.durations]),'risk':risk.risk})
                lr=e['lattice_shrinkage'][ci].get('duration_ms',{}) if ci<len(e['lattice_shrinkage']) else {}
                ef=e['edge_native']['decisions'][ci] if ci<len(e['edge_native']['decisions']) else {}
                selected_features=frozenset(lr.get('context',())[1:]);key=tuple(sorted(selected_features))
                if key not in source_cache:
                    exact_sources=[o for o,f in train if f.features==cf.features] if selected_features==cf.features else []
                    matching=exact_sources or [o for o,f in train if selected_features and selected_features<=f.features]
                    grouped=defaultdict(list)
                    for o in matching:grouped[(o.repo,tuple(o.argv))].append(o.latency_ms)
                    source_cache[key]=[{'repo':repo,'argv':list(argv),'durations_ms':sorted(v)} for (repo,argv),v in sorted(grouped.items())]
                details.append({'clause_index':ci,'argv':cl['argv'],'parsed':parsed.to_dict(),'features':cf.to_dict(),
                    'lattice':{'selected_features':sorted(selected_features),'selected_values_ms':lr.get('values',()),
                               'exact':selected_features==cf.features,'candidates':candidate_rows,'historical_sources':source_cache[key]},
                    'edge':ef})
            with (dataset/c['trace']['file']).open() as f:
                raw=next(json.loads(line) for i,line in enumerate(f,1) if i==c['trace']['line'])
            raw_clauses=(raw['data'].get('resource_observation') or {}).get('clauses',[])
            measured=[{'argv':x.get('argv'),'latency_ms':x.get('latency_ms'),'eligible_for_kb':x.get('eligible_for_kb'),
                       'in_loop':x.get('in_loop'),'in_subst':x.get('in_subst')} for x in raw_clauses]
            partial=any(d['parsed']['status']!='complete' for d in details)
            if model=='edge_native':
                coarse=any(not d['edge'].get('exact_match') for d in details)
                sparse=any(d['edge'].get('observation_count',0)<5 for d in details)
            else:
                coarse=any(not d['lattice']['exact'] for d in details)
                sparse=any(len(d['lattice']['selected_values_ms'])<5 for d in details)
            primary='C' if coarse or sparse or partial else 'B'
            reason=('partial parser and/or unseen/sparse signature leads to generalized historical evidence' if primary=='C'
                    else 'exact represented signature still has duration heterogeneity or hidden workload changes')
            hypotheses=['D: workload size, cache, execution state and hook overhead cannot be separated causally by this trace alone']
            if partial:hypotheses.append('C: missing/partial grammar leaves command arguments opaque; inspect parsed diagnostics')
            item={'rank':rank,**r,'absolute_error_s':abs(r['p50']-r['actual_ms'])/1000,
                  'log_error':abs(math.log1p(r['p50']/1000)-math.log1p(r['actual_ms']/1000)),
                  'bucket_distance':abs(r['predicted_bucket']-r['actual_bucket']), 'clauses':details,'measured_clauses':measured,
                  'raw_exit_code':raw['data'].get('exit_code'),'raw_success':raw['data'].get('success'),
                  'output_tail':str(raw['data'].get('tool_result',''))[-1800:],
                  'attribution':{'primary':primary,'direct':reason,'confirmed_implementation_bug':False,
                     'causal_limitations':hypotheses,'repair_hypothesis':'grammar completion and source-only transferable parameter effects' if primary=='C' else 'workload observability or robust context calibration',
                     'evidence':'actual parser output, selected nodes, all legal Lattice candidates, Edge active parents, and historical duration records attached'}}
            result.append(item);progress('worst_case',model=model,rank=rank)
        write(out/(model+'-worst30.json.gz'),result);all_details[model]=Counter(x['attribution']['primary'] for x in result)
    write(out/'attribution-counts.json',{'top30_primary':{m:dict(c) for m,c in all_details.items()},
           'scope':'60 error cases, not an estimate of all-call bug prevalence; D causal factors unresolved; no bug asserted without reproduction'})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['ablations','worst']);p.add_argument('--out',type=Path,default=HERE/'results');p.add_argument('--baseline',type=Path,default=Path('baseline'));a=p.parse_args()
    ablations(a.out) if a.phase=='ablations' else worst(a.out,a.baseline)
