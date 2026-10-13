"""Diagnostic hindsight oracle over existing duration candidate nodes.

Uses held-out actuals only to measure selection headroom, never to train a KB.
"""
import gzip
import itertools
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from clawtune_sidecar.predictors.call_load import compose
from clawtune_sidecar.prediction_config import load_bucket_edges
from command_parser.models import CommandFeatures
from command_parser.service import observation_features
from tool_resource.runtime_kb import is_pipeline_dependent_consumer
from tool_time.lattice_kb import LatticeTimeKB, _effective_max_optional_features
from tool_time.query import query_nodes
from tool_time.resource_lattice import nonnegative

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
EDGES = load_bucket_edges((100,500,2000,10000))
cases = json.loads(gzip.decompress((OUT/'cases.json.gz').read_bytes()))
kb = LatticeTimeKB.from_json_obj(json.loads((ROOT/'.runtime/offline/kb-methods-20261012/seed/clause-lattice-time-kb.json').read_bytes()))
kb.freeze()
state = kb._resource_states['load:duration_ms']
training = [(o,observation_features(o)) for o in kb._observations
            if nonnegative(o.latency_ms) and not o.in_loop and not o.in_subst and not is_pipeline_dependent_consumer(o)]
maximum = _effective_max_optional_features([SimpleNamespace(command_features=f.to_dict()) for o,f in training])
print('BUILD',len(training),'observations',len(state.nodes),'nodes','effective_max_optional_features',maximum,flush=True)


def node_record(fs, node, own=False):
    return dict(features=sorted(fs),count=node.count,p50_ms=node.median_s*1000,
                values=sorted(v*1000 for v in node.durations),uses_own=own)


def best_call(candidate_sets, actual):
    size = math.prod(len(c) for c in candidate_sets)
    assert size <= 100000, size
    if len(candidate_sets)==1:
        selected = min(candidate_sets[0],key=lambda r:(abs(r['p50_ms']-actual),-len(r['features'])))
        return dict(p50_ms=selected['p50_ms'],absolute_error_ms=abs(selected['p50_ms']-actual),
                    features=[selected['features']],combinations=size)
    best = None
    for candidates in itertools.product(*candidate_sets):
        result = compose('lattice',[{'duration_ms':dict(values=c['values'])} for c in candidates],
                         EDGES).targets['duration_ms']
        error = abs(result.p50-actual)
        if best is None or error<best['absolute_error_ms']:
            best = dict(p50_ms=result.p50,absolute_error_ms=error,features=[c['features'] for c in candidates],combinations=size)
    return best


records=[]
for c in cases:
    legal_sets, relaxed_sets, details = [],[],[]
    for d in c['clauses']:
        q=CommandFeatures.from_dict(d['features'])
        legal=query_nodes(state.nodes,q,global_log_std=state.global_log_std)
        exact_own=q.features in legal and bool(legal[q.features].own_durations) and len(legal)==1
        relaxed={fs:node for fs,node in state.nodes.items() if q.core<=fs<=q.features}
        if exact_own:
            relaxed[q.features]=legal[q.features]
        current=[node_record(fs,n,exact_own) for fs,n in legal.items()]
        expanded=[node_record(fs,n,exact_own and fs==q.features) for fs,n in relaxed.items()]
        legal_sets.append(current)
        relaxed_sets.append(expanded)
        arg_evidence=[]
        for arg in sorted(f for f in q.features if f.startswith('arg:')):
            vals=json.loads(arg[4:]); path=vals[-1]
            # Repo identity is already available explicitly among query features.
            repo_features={f for f in q.features if f.startswith('context:["repo",')}
            eligible=[(o,f) for o,f in training if q.core<=f.features and repo_features<=f.features]
            same_position=[o.latency_ms for o,f in eligible if arg in f.features]
            any_position=[o.latency_ms for o,f in eligible if any(x.startswith('arg:') and json.loads(x[4:])[-1]==path for x in f.features)]
            probe=q.core|repo_features|{arg}
            arg_evidence.append(dict(feature=arg,same_repo_same_position_count=len(same_position),
                same_repo_any_position_count=len(any_position),
                same_position_p50_ms=statistics.median(same_position) if same_position else None,
                same_position_range_ms=[min(same_position),max(same_position)] if same_position else None,
                core_repo_argument_node_exists=probe in state.nodes))
        selected=[r for r in current if r['features']==d['selected_features']]
        assert len(selected)==1
        assert math.isclose(selected[0]['p50_ms'],d['p50_ms'],abs_tol=1e-6)
        details.append(dict(argv=d['argv'],query=d['features'],current_features=d['selected_features'],
            current_p50_ms=d['p50_ms'],legal_count=len(current),relaxed_count=len(expanded),
            legal_candidates=[{k:v for k,v in r.items() if k!='values'} for r in current],
            relaxed_candidates=[{k:v for k,v in r.items() if k!='values'} for r in expanded],argument_evidence=arg_evidence))
    # These audited chains are serial; retained stages remain unchanged.
    assert all(not x.get('in_pipe') for x in c['recorded_clauses'])
    baseline=[next(r for r in choices if r['features']==d['selected_features']) for d,choices in zip(c['clauses'],legal_sets,strict=True)]
    if len(baseline)>1:
        p=compose('lattice',[{'duration_ms':dict(values=r['values'])} for r in baseline],EDGES).targets['duration_ms'].p50
        assert math.isclose(p,c['prediction']['p50'],abs_tol=1e-6),(c['rank'],p,c['prediction']['p50'])
    oracle=best_call(legal_sets,c['actual_ms'])
    relaxed_oracle=best_call(relaxed_sets,c['actual_ms'])
    assert oracle['absolute_error_ms']<=c['absolute_error_ms']+1e-6
    records.append(dict(rank=c['rank'],id=c['id'],actual_ms=c['actual_ms'],current_p50_ms=c['prediction']['p50'],
        current_absolute_error_ms=c['absolute_error_ms'],cause=c['primary_cause'],legal_oracle=oracle,
        relaxed_exact_oracle=relaxed_oracle,clauses=details))
    print(c['rank'],'counts',[(d['legal_count'],d['relaxed_count']) for d in details],
          'seconds',*[round(x/1000,3) for x in (c['actual_ms'],c['prediction']['p50'],oracle['p50_ms'],relaxed_oracle['p50_ms'])],flush=True)
    (OUT/'oracle-progress.json').write_text(json.dumps(records,ensure_ascii=False),encoding='utf8')

result=dict(definition='Exact hindsight minimization of call p50 absolute error over Cartesian product of existing candidate nodes; production serial composer and fixed seed retained. Relaxed oracle permits subset parents on exact matches, retaining own samples for exact node.',
            effective_max_optional_features=maximum,training_observations=len(training),nodes=len(state.nodes),calls=records)
(OUT/'oracle.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
for label,subset in [('all30',records),('generalized17',[r for r in records if r['cause']=='测试范围等信息在泛化时丢失'])]:
    base=sum(r['current_absolute_error_ms'] for r in subset)
    print(label,'baselineAE',base/1000,'legalAE',sum(r['legal_oracle']['absolute_error_ms'] for r in subset)/1000,
          'relaxedAE',sum(r['relaxed_exact_oracle']['absolute_error_ms'] for r in subset)/1000,flush=True)
