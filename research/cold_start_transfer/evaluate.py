"""Repository-disjoint feasibility experiments using the unmodified KBs."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import shlex
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'services/sidecar/src')]
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.edge_kappa_adapter import shell_query
from edge_kappa_kb import EdgeKappaKB, TimeOutcome, TrainingEvent

SEED=20261011
NS=(0,1,2,5,10,20)
SCALES={'duration_ms':1000., 'sampled_peak_rss_bytes':1048576.,
        'memory_extra_peak_bytes':1048576., 'cpu_time_seconds':1.}
METHODS=('global','executable','family','family_calibrated','trie','lattice',
         'lattice_no_repo','lattice_loso','lattice_max_cardinality','lattice_own_nodes',
         'edge','edge_fixed','edge_freeze_target')


def dump(path, data):
    path.write_text(json.dumps(data,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')


def csv_write(path, rows, fields=None):
    fields=fields or sorted({k for r in rows for k in r})
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def family(row):
    """Transparent syntax grouping, not an inferred resource-effect model."""
    tool=row['tool'];argv=row['observation']['argv']; suffix=''
    if tool in {'git','pip','npm','cargo','go','docker'}:
        suffix=next((a for a in argv[1:] if not a.startswith('-')),'')
    elif tool=='python':
        if '-m' in argv:
            i=argv.index('-m');suffix='module:'+argv[i+1] if i+1<len(argv) else 'module:missing'
        elif '-c' in argv:suffix='inline'
        elif any(a.endswith('.py') for a in argv[1:]):suffix='script'
        else:suffix='interpreter'
    return tool+(':'+suffix if suffix else '')


def condition(row):
    hardware=row['hardware']
    return '|'.join(map(str,(row['stratum'],hardware.get('host_arch'),hardware.get('tool_arch'),
                           hardware.get('cpu_quota'),hardware.get('emulation'),hardware.get('parallelism'),
                           row['observation'].get('memory_measurement'))))


def quantiles(values, weights=None):
    if not values:return None
    if weights is None:
        values=sorted(values)
        return st.median(values),values[math.ceil(.9*len(values))-1]
    ordered=sorted(zip(values,weights));total=sum(weights);out=[]
    for q in (.5,.9):
        cum=0.
        for i,(value,weight) in enumerate(ordered):
            cum+=weight
            if cum >= q*total or math.isclose(cum,q*total,rel_tol=1e-12):
                if q==.5 and math.isclose(cum,q*total,rel_tol=1e-12) and i+1<len(ordered):
                    value=(value+ordered[i+1][0])/2
                out.append(value);break
    return tuple(out)


def split_prefix(rows):
    """First measured executions; hold out whole tasks, including all attempts."""
    tasks=defaultdict(list)
    for row in rows:tasks[row['task']].append(row)
    ordered=sorted(tasks,key=lambda t:(min(r['observation']['ts_start'] for r in tasks[t]),t))
    flattened=[r for task in ordered for r in sorted(tasks[task],key=lambda r:(r['observation']['ts_start'],r['id']))]
    supported=[0]
    for n in NS[1:]:
        if n>=len(flattened):continue
        touched={r['task'] for r in flattened[:n]}
        if any(r['task'] not in touched for r in flattened[n:]):supported.append(n)
    max_n=max(supported);prefix=flattened[:max_n];touched={r['task'] for r in prefix}
    test=[r for r in flattened if r['task'] not in touched]
    assert test and not ({r['task'] for r in prefix}&{r['task'] for r in test})
    return prefix,test,supported


def empirical(training,target):
    pools=defaultdict(list)
    for row in training:
        y=row['values'][target]
        pools[('global','')].append(y)
        pools[('executable',row['tool'])].append(y)
        pools[('family',family(row))].append(y)
    return {key:(quantiles(values),len(values)) for key,values in pools.items()}


def residuals(prefix,pools,target):
    groups=defaultdict(list);scale=SCALES[target]
    for row in prefix:
        pred=pools.get(('family',family(row)))
        if pred:
            groups[family(row)].append(math.log1p(row['values'][target]/scale)-math.log1p(pred[0][0]/scale))
    return groups


def adjust(pred, residual, smooth, scale):
    if not residual:return pred
    bias=st.median(residual)*len(residual)/(len(residual)+smooth)
    return tuple(max(0.,math.expm1(math.log1p(value/scale)+bias)*scale) for value in pred)


def choose_smoothing(source,target):
    """Nested source-repository validation only; equal repository weight."""
    losses={k:[] for k in (0,2,5)};details=[]
    for repo in sorted({r['repo'] for r in source}):
        train=[r for r in source if r['repo']!=repo];validation=[r for r in source if r['repo']==repo]
        prefix,test,ns=split_prefix(validation)
        if not prefix or not train:continue
        pools=empirical(train,target);per={k:[] for k in losses}
        for n in ns[1:]:
            residual=residuals(prefix[:n],pools,target)
            for row in test:
                pred=pools.get(('family',family(row)))
                if pred is None:continue
                for k in losses:
                    estimate=adjust(pred[0],residual[family(row)],k,SCALES[target]);y=row['values'][target]
                    loss=(abs(math.log1p(y/SCALES[target])-math.log1p(estimate[0]/SCALES[target]))
                          if target=='duration_ms' else pinball(y,estimate[1])/SCALES[target])
                    per[k].append(loss)
        for k in losses:
            if per[k]:losses[k].append(st.mean(per[k]))
        if per[0]:details.append(repo)
    # No inner data: predeclared conservative smoothing, explicitly unvalidated.
    selected=min(losses,key=lambda k:(st.mean(losses[k]) if losses[k] else float('inf'),-k))
    return selected,dict(repositories=details,mean_losses={str(k):st.mean(v) if v else None for k,v in losses.items()},
                         status='validated' if details else 'no_inner_few_shot_data_default_5')


def observation(row,target,index,remove_repo=False):
    raw=dict(row['observation']);raw['argv']=tuple(raw['argv'])
    raw.update(ts_start=float(index*2),ts_end=float(index*2+1),repo='' if remove_repo else row['repo'])
    # Keep target-specific sample budgets; no extra local labels from another target.
    if target!='duration_ms':raw['latency_ms']=None
    if target!='sampled_peak_rss_bytes':raw['sampled_peak_rss_mb']=None
    if target!='cpu_time_seconds':raw['cpu_ns_cumulative']=None
    raw['cpu_peak_cores']=None
    if target!='memory_extra_peak_bytes':
        raw.update(memory_eligible=False,memory_baseline_bytes=None,memory_total_peak_bytes=None,
                   memory_extra_peak_bytes=None,memory_measurement=None,memory_environment_id=None)
    return ClauseObservation(**raw)


def event(row,index):
    obs=row['observation'];query=shell_query(shlex.join(obs['argv']),repo=row['repo'])
    outcome=TimeOutcome(row['id'],index*2.,index*2.+1,row['task'],row['call_id'],row['id'],
                        duration_ms=row['values']['duration_ms'],label_source='clause')
    return TrainingEvent(query,outcome,shlex.join(obs['argv']))


def pinball(y,q):return (.9-float(y<q))*(y-q)


def metrics(rows):
    predicted=[r for r in rows if r['p50'] is not None]
    result=dict(eligible=len(rows),predicted=len(predicted),availability=len(predicted)/len(rows) if rows else None,
                abstention=1-len(predicted)/len(rows) if rows else None)
    for name in ('mae','log_mae','p90_pinball','p90_coverage'):
        result[name]=st.mean(r[name] for r in predicted) if predicted else None
    return result


def run_fold(rows,repo,target,output):
    training=sorted([r for r in rows if r['repo']!=repo],key=lambda r:(r['task'],r['observation']['ts_start'],r['id']))
    local=[r for r in rows if r['repo']==repo]
    prefix,test,ns=split_prefix(local)
    assert {r['repo'] for r in training}.isdisjoint({repo})
    assert not ({r['task'] for r in training}&{r['task'] for r in local})
    pools=empirical(training,target);smoothing,validation=choose_smoothing(training,target)
    source=[observation(r,target,i) for i,r in enumerate(training)]
    models={'lattice':LatticeTimeKB.fit(source),'lattice_no_repo':LatticeTimeKB.fit([replace(o,repo='') for o in source]),
            'lattice_own_nodes':LatticeTimeKB.fit(source,subset_coverage=False),
            'trie':ClauseResourceKB.fit_public(source)}
    if target=='duration_ms':
        events=[event(r,i) for i,r in enumerate(training)]
        models['edge']=EdgeKappaKB.fit(events,{'replay_seed':SEED})
        models['edge_fixed']=EdgeKappaKB.fit(events,{'replay_seed':SEED,'learn_weights':False})
        models['edge_freeze_target']=copy.deepcopy(models['edge']);models['edge_freeze_target'].learn_weights=False
    predictions=[];done=0
    for n in ns:
        for i in range(done,n):
            row=prefix[i];index=len(training)+i+1
            for name,model in models.items():
                if name.startswith('edge'):
                    e=event(row,index);model.begin(e.query,e.outcome.event_id,e.outcome.start_time,
                        task_id=e.outcome.task_id,call_id=e.outcome.call_id,clause_id=e.outcome.clause_id)
                    model.complete(e.outcome.event_id,e.outcome)
                else:model.observe_completed_clause(observation(row,target,index,name=='lattice_no_repo'))
        done=n;timestamp=float(2*(len(training)+n+2));residual=residuals(prefix[:n],pools,target)
        for name,model in models.items():
            if name.startswith('edge'):model.commit_before(timestamp)
        cache={}
        for row in test:
            argv=tuple(row['observation']['argv']);scale=SCALES[target];y=row['values'][target]
            for method in METHODS:
                if method.startswith('edge') and target!='duration_ms':continue
                key=(method,argv,row['observation'].get('memory_measurement'))
                if key not in cache:
                    pred=None;evidence=0;context=[];reason=None;diagnostics={}
                    if method in {'global','executable','family','family_calibrated'}:
                        kind='family' if method=='family_calibrated' else method
                        group='' if kind=='global' else row['tool'] if kind=='executable' else family(row)
                        selected=pools.get((kind,group))
                        if selected:pred,evidence=selected;context=[kind,group]
                        if pred and method=='family_calibrated':
                            if n>0 and validation['status']!='validated':
                                pred=None;reason='no_source_only_calibration_validation'
                            else:pred=adjust(pred,residual[family(row)],smoothing,scale)
                        if not pred and reason is None:reason='no_compatible_source_history'
                    elif method.startswith('edge'):
                        d=models[method].predict_duration(shell_query(shlex.join(argv),repo=repo))
                        pred=quantiles(d.values_ms,d.weights);evidence=d.evidence_count;reason=d.unavailable_reason
                        p=d.bucket_prediction;context=[p.node_id]
                        diagnostics=dict(exact_match=p.exact_match,own_count=p.observation_count,
                                         overlap=p.evidence_overlap_ratio,parent_count=len(p.evidence),
                                         dropped_features=sorted({f for x in p.evidence for f in x.dropped_features}))
                    else:
                        model=models['lattice'] if method in {'lattice_loso','lattice_max_cardinality'} else models[method]
                        clause=dict(row['observation'],argv=argv)
                        if clause.get('memory_measurement') is None:clause.pop('memory_measurement',None)
                        kwargs={} if method=='trie' else {'algorithm':method.removeprefix('lattice_') if method in {'lattice_loso','lattice_max_cardinality'} else 'shrinkage'}
                        selected=model.predict_load_samples('' if method=='lattice_no_repo' else repo,[clause],timestamp,**kwargs)
                        selected=selected[0].get(target,{}) if selected else {}
                        values=selected.get('values',());pred=quantiles(values);evidence=len(values)
                        context=selected.get('context',[]);reason=selected.get('unavailable_reason') if pred else selected.get('unavailable_reason','no_matching_evidence')
                    cache[key]=(pred,evidence,context,reason,diagnostics)
                pred,evidence,context,reason,diagnostics=cache[key]
                p50,p90=pred if pred else (None,None)
                predictions.append(dict(id=row['id'],repo=repo,task=row['task'],target=target,n=n,method=method,
                    family=family(row),actual=y,p50=p50,p90=p90,evidence=evidence,context=context,
                    reason=reason,diagnostics=diagnostics,calibration_family_count=len(residual[family(row)]),
                    mae=abs(y-p50) if pred else None,
                    log_mae=abs(math.log1p(y/scale)-math.log1p(p50/scale)) if pred else None,
                    p90_pinball=pinball(y,p90) if pred else None,p90_coverage=float(y<=p90) if pred else None))
    split=dict(repo=repo,target=target,training_ids=[r['id'] for r in training],prefix_ids=[r['id'] for r in prefix],
               test_ids=[r['id'] for r in test],supported_n=ns,smoothing=smoothing,inner_validation=validation,
               task_disjoint=True,repository_disjoint=True,clock='simulated_serial_source_then_target',
               temporal_gate_verified=all(2*(len(training)+i+1)+1 < 2*(len(training)+n+2) for n in ns for i in range(n)))
    split['evaluator_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    dump(output/'split.json',split)
    with (output/'predictions.jsonl').open('w',encoding='utf-8') as f:
        for p in predictions:f.write(json.dumps(p,sort_keys=True,allow_nan=False)+'\n')
    return predictions


def experiment_one(records,output):
    groups=defaultdict(list)
    for row in records:
        for target in SCALES:
            if target in row['values']:groups[(condition(row),family(row),row['repo'] or 'unidentified:'+row['task'],target)].append(row)
    rows=[];repeats=[];pairs=[]
    for (stratum,fam,repo,target),samples in sorted(groups.items()):
        values=[r['values'][target] for r in samples];p50,p90=quantiles(values)
        rows.append(dict(stratum=stratum,family=fam,repo=repo,target=target,n=len(values),p50=p50,p90=p90,
                         log_std=st.stdev(math.log1p(y/SCALES[target]) for y in values) if len(values)>1 else None))
        repeated=defaultdict(list)
        for row in samples:repeated[(row['task'],tuple(row['observation']['argv']))].append(row)
        for (task,argv),same in repeated.items():
            if len(same)>1:
                repeats.append(dict(stratum=stratum,repo=repo,target=target,task=task,command=shlex.join(argv),n=len(same),
                    log_std=st.stdev(math.log1p(r['values'][target]/SCALES[target]) for r in same),
                    interpretation='observational_repeat_including_code_state_changes'))
        # Only pytest workers/coverage variants: keep every other argv token and task identical.
        matched=defaultdict(lambda:defaultdict(list))
        for row in samples:
            if row['tool']!='pytest':continue
            argv=row['observation']['argv'];base=[];workers='default';coverage=[];i=0
            while i<len(argv):
                a=argv[i]
                if a in {'-n','--numprocesses'} and i+1<len(argv):workers=argv[i+1];i+=2;continue
                if a.startswith('--numprocesses='):workers=a.split('=',1)[1];i+=1;continue
                if a.startswith('--cov'):
                    coverage.append(a)
                    if a=='--cov' and i+1<len(argv) and not argv[i+1].startswith('-'):coverage.append(argv[i+1]);i+=1
                    i+=1;continue
                base.append(a);i+=1
            matched[(row['task'],tuple(base))][(workers,tuple(coverage))].append(row)
        for (task,base),variants in matched.items():
            keys=sorted(variants)
            for i,reference in enumerate(keys):
                for variant in keys[i+1:]:
                    # One parameter category at a time; input/code-state verification still required.
                    if sum(a!=b for a,b in zip(reference,variant))!=1:continue
                    ref=variants[reference];var=variants[variant]
                    delta=math.log1p(st.median(r['values'][target] for r in var)/SCALES[target])-math.log1p(st.median(r['values'][target] for r in ref)/SCALES[target])
                    pairs.append(dict(stratum=stratum,repo=repo,task=task,target=target,base=shlex.join(base),
                        reference=str(reference),variant=str(variant),delta=delta,n_reference=len(ref),n_variant=len(var),
                        status='candidate_only_code_state_and_input_size_unverified',
                        reference_ids=json.dumps([r['id'] for r in ref]),variant_ids=json.dumps([r['id'] for r in var])))
    csv_write(output/'e1_transferability.csv',rows)
    csv_write(output/'e1_repeat_noise.csv',repeats)
    csv_write(output/'e1_parameter_pairs.csv',pairs,['stratum','repo','task','target','base','reference','variant','delta','n_reference','n_variant','status','reference_ids','variant_ids'])
    return rows


def summarize(predictions,output):
    grouped=defaultdict(list)
    for p in predictions:grouped[(p['stratum'],p['repo'],p['target'],p['n'],p['method'])].append(p)
    summary=[];paired=[]
    for (stratum,repo,target,n,method),rows in sorted(grouped.items()):
        summary.append(dict(stratum=stratum,repo=repo,target=target,n=n,method=method,**metrics(rows)))
        base={p['id']:p for p in grouped[(stratum,repo,target,n,'lattice')]}
        common=[p for p in rows if p['p50'] is not None and base[p['id']]['p50'] is not None]
        if common:
            d=[p['log_mae']-base[p['id']]['log_mae'] for p in common]
            paired.append(dict(stratum=stratum,repo=repo,target=target,n=n,method=method,paired=len(common),
                delta_log_mae=st.mean(d),delta_mae=st.mean(p['mae']-base[p['id']]['mae'] for p in common),
                delta_p90_pinball=st.mean(p['p90_pinball']-base[p['id']]['p90_pinball'] for p in common),
                worse_fraction=st.mean(x>1e-12 for x in d),better_fraction=st.mean(x< -1e-12 for x in d)))
    csv_write(output/'e2_cold_start.csv',summary)
    csv_write(output/'e3_kb_ablation.csv',paired)
    # Calibration compared directly to its own uncalibrated source prior.
    changes=[]
    for (stratum,repo,target,n,method),rows in sorted(grouped.items()):
        if method!='family_calibrated':continue
        base={p['id']:p for p in grouped[(stratum,repo,target,n,'family')]}
        matched=[p for p in rows if p['p50'] is not None and base[p['id']]['p50'] is not None]
        if matched:
            changes.append(dict(stratum=stratum,repo=repo,target=target,n=n,paired=len(matched),
                delta_log_mae=st.mean(p['log_mae']-base[p['id']]['log_mae'] for p in matched),
                delta_p90_pinball=st.mean(p['p90_pinball']-base[p['id']]['p90_pinball'] for p in matched),
                adjusted=sum(p['calibration_family_count']>0 for p in matched)))
    csv_write(output/'calibration_effect.csv',changes)
    return summary,paired


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    if args.output.exists() and not args.resume:raise ValueError('use a new output directory or --resume')
    args.output.mkdir(parents=True,exist_ok=True)
    records=[json.loads(line) for line in (args.data/'observations.jsonl').open(encoding='utf-8')]
    experiment_one(records,args.output)
    strata=defaultdict(list)
    for row in records:
        if row['repo'] is not None:strata[condition(row)].append(row)
    predictions=[];skipped=[]
    for stratum,rows in sorted(strata.items()):
        if len({r['repo'] for r in rows})<2:
            skipped.append(dict(stratum=stratum,reason='fewer_than_two_repositories',observations=len(rows)));continue
        for target in SCALES:
            eligible=[r for r in rows if target in r['values']]
            repos=sorted({r['repo'] for r in eligible})
            if len(repos)<2:
                skipped.append(dict(stratum=stratum,target=target,reason='fewer_than_two_labeled_repositories'));continue
            for repo in repos:
                key=hashlib.sha256(json.dumps([stratum,repo,target]).encode()).hexdigest()[:16]
                folder=args.output/'folds'/key;folder.mkdir(parents=True,exist_ok=True)
                print('FOLD',stratum,repo,target,'rows',len(eligible),flush=True)
                started=time.monotonic()
                if args.resume and (folder/'predictions.jsonl').exists() and (folder/'split.json').exists():
                    previous=json.loads((folder/'split.json').read_text(encoding='utf-8'))
                    expected_prefix,expected_test,_=split_prefix([r for r in eligible if r['repo']==repo])
                    if (previous.get('evaluator_sha256')!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                        or set(previous['training_ids'])!={r['id'] for r in eligible if r['repo']!=repo}
                        or previous['prefix_ids']!=[r['id'] for r in expected_prefix]
                        or previous['test_ids']!=[r['id'] for r in expected_test]):
                        raise ValueError('resume code/data/split mismatch; use a new output directory')
                    result=[json.loads(line) for line in (folder/'predictions.jsonl').open(encoding='utf-8')]
                else:result=run_fold(eligible,repo,target,folder)
                for p in result:p['stratum']=stratum
                predictions.extend(result)
                print('DONE',key,len(result),round(time.monotonic()-started,2),'seconds',flush=True)
    summarize(predictions,args.output)
    dump(args.output/'skipped.json',skipped)
    dump(args.output/'config.json',dict(seed=SEED,n=NS,scales=SCALES,methods=METHODS,
        source_sha256=hashlib.sha256((args.data/'observations.jsonl').read_bytes()).hexdigest(),
        protocol='LORO; target-specific prefix; task-disjoint common test; simulated serial clocks',
        public_seed_loaded=False,calibration_smoothing_candidates=[0,2,5]))
    print('COMPLETE',len(predictions),'predictions',flush=True)


if __name__=='__main__':main()
