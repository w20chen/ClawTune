"""Existing RuleGenerator -> schema validation -> parser -> rebuilt isolated KBs."""
from collections import Counter, defaultdict
from dataclasses import replace
import argparse
import copy
import json
from pathlib import Path
import shlex
import time

from research.kb_systematic_20261013.study import HERE,EDGES,read,write,progress,fingerprint,metrics,estimate_row

def generate(out):
    from command_parser import CommandFeatureService,ParseContext,default_service
    from command_parser.generation import RuleGenerator,GeneratorConfig
    from command_parser.registry import RuleRegistry
    from command_parser.help_runner import HelpRunner
    from research.kb_systematic_20261013.provider import Provider
    calls=[c for c in read(out/'calls.json.gz') if c['partition']=='train']
    counts=Counter();examples={}
    for c in calls:
        for cl in c['clauses']:
            parsed=default_service().parser.parse(cl.get('syntax_argv',cl['argv']),ParseContext(repo=c['repo']))
            for d in parsed.diagnostics:
                if d.startswith('missing_rule:'):
                    entry=d.split(':',1)[1];counts[entry]+=1;examples.setdefault(entry,cl.get('syntax_argv',cl['argv']))
    # Choose from actual source-training commands; the shell builtin `command`
    # cannot be acquired as a standalone executable and is explicitly recorded.
    entries=[k for k in ('apt-get','apt-cache','unittest','ensurepip','which','sort') if counts[k]]
    provider=Provider(out/'llm/rules');provider.models()
    registry=RuleRegistry(out/'llm/rule-cache');config=GeneratorConfig(provider.base,provider.key,provider.model,timeout=180)
    help_runner=HelpRunner();generated=[];failures=[]
    for entry in entries:
        context=ParseContext(repo='swe-rebench:tobymao/sqlglot',runtime_id='study-source',image='cube-sandbox-builder:ubuntu2004')
        dest=out/'llm/rules'/('rule-'+entry+'.json')
        if dest.exists():generated.append(read(dest));continue
        gen=RuleGenerator(registry,config,client=provider,help_runner=help_runner)
        service=CommandFeatureService(registry,gen);argv=examples[entry]
        try:
            before=service.argv(argv,context).to_dict()
            # Actual service triggers asynchronous generation when it encounters missing rule.
            deadline=time.monotonic()+380
            while time.monotonic()<deadline:
                states=gen.status()
                if states and all(s['status'] in ('ready','failed') for s in states.values()):break
                time.sleep(.25)
            after=service.argv(argv,context).to_dict();rule,_=registry.lookup(entry,context)
            row={'entry':entry,'training_count':counts[entry],'argv':argv,'before':before,'after':after,'jobs':gen.status(),
                 'parsed_before':default_service().parser.parse(argv,context).to_dict(),'parsed_after':service.parser.parse(argv,context).to_dict()}
            if rule and rule['origin']['kind']=='llm':
                write(dest,rule);generated.append(rule);row['status']='ready'
            else:row['status']='failed';failures.append(row)
            write(out/'llm/rules'/('activation-'+entry+'.json'),row)
            progress('rule',entry=entry,status=row['status'])
        finally:service.close()
    write(out/'llm/rule-generation-summary.json',{'requested':entries,'ready':len(generated),'failed':failures,
          'unknown_training_counts':dict(counts),'builtin_command':'missing rule detected; standalone help acquisition unavailable; not silently replaced',
          'model':provider.model,'prompt':'existing command_parser.generation.RuleGenerator system prompt',
          'labels_sent':False,'help_image':'cube-sandbox-builder:ubuntu2004'})

def service_for(out,repo):
    from command_parser import CommandFeatureService
    from command_parser.registry import RuleRegistry
    reg=RuleRegistry()
    for p in sorted((out/'llm/rules').glob('rule-*.json')):
        rule=read(p);rule['scope']=dict(rule['scope'],repo=repo,runtime_id='study-rules')
        reg.add(rule,persist=False)
    return CommandFeatureService(reg)

def reparse_calls(calls,out):
    from command_parser import ParseContext
    services={};new=[];changes=[]
    for c in calls:
        svc=services.setdefault(c['repo'],service_for(out,c['repo'])) if c['repo'] not in services else services[c['repo']]
        context=ParseContext(repo=c['repo'],runtime_id='study-rules')
        cls=svc.prepare(c['clauses'],context);n=dict(c,clauses=cls);new.append(n)
        for i,(a,b) in enumerate(zip(c['clauses'],cls)):
            parsed=svc.parser.parse(b.get('syntax_argv',b['argv']),context)
            if a.get('command_features')!=b.get('command_features'):
                changes.append({'id':c['id'],'repo':c['repo'],'clause_index':i,'argv':b['argv'],
                    'before':a.get('command_features'),'after':b.get('command_features'),'parsed_after':parsed.to_dict()})
    return new,changes

def evaluate(out):
    from command_parser import ParseContext
    from command_parser.service import observation_features
    from tool_resource.runtime_kb import ClauseObservation
    from tool_time.lattice_kb import LatticeTimeKB
    from edge_kappa_kb import EdgeKappaKB,TrainingEvent
    from tool_time.edge_kappa_adapter import feature_query
    from clawtune_sidecar.predictors.call_load import compose
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from research.kb_systematic_20261013.optimization import event_obj,fit_model,evaluate as edge_evaluate
    if not (out/'selection-lock.json').exists():raise RuntimeError('primary tuning must be locked before test')
    calls=read(out/'calls.json.gz');train_obs=[r for r in read(out/'observations.json.gz') if r['partition']=='train']
    event_rows=[r for r in read(out/'events.json.gz') if r['partition']=='train']
    services={};new_obs=[];original_obs=[]
    for r in train_obs:
        data=r['observation'];o=ClauseObservation(**dict(data,argv=tuple(data['argv'])));original_obs.append(o)
        if o.repo not in services:services[o.repo]=service_for(out,o.repo)
        f=services[o.repo].argv(o.argv,ParseContext(repo=o.repo,runtime_id='study-rules'))
        new_obs.append(replace(o,command_features=f.to_dict()))
    new_events=[]
    for r in event_rows:
        ev=event_obj(r);repo=next(f for f in ev.query.features if f.startswith('context:["repo",'))
        repo=json.loads(repo.split(':',1)[1])[1]
        if repo not in services:services[repo]=service_for(out,repo)
        f=services[repo].argv(shlex.split(ev.source_command),ParseContext(repo=repo,runtime_id='study-rules'))
        new_events.append(TrainingEvent(feature_query(f),ev.outcome,ev.source_command))
    write(out/'llm/rule-policy.json',{'scope':'same generated rule body explicitly instantiated into each experimental repository/runtime; no automatic cross-repository deployment claim',
          'training_events':len(new_events),'training_observations':len(new_obs),
          'features_materialized_training':len(new_obs),
          'features_changed_training':sum(observation_features(a).features!=observation_features(b).features for a,b in zip(original_obs,new_obs)),
          'no_labels_in_prompts':True})
    progress('llm_fit_lattice_original')
    lat_a=LatticeTimeKB.fit(original_obs,max_optional_features=6);lat_a.freeze();lat_a.prepare()
    progress('llm_fit_lattice_generated')
    lat_b=LatticeTimeKB.fit(new_obs,max_optional_features=6);lat_b.freeze();lat_b.prepare()
    edge_a=EdgeKappaKB.from_snapshot(read(out/'optimization/native/model.json.gz'));edge_a.freeze()
    progress('llm_fit_edge_generated')
    edge_b=fit_model(new_events,{'learning_rate':.1})
    write(out/'llm/generated-edge.json.gz',edge_b.to_snapshot())
    summary={};edges=load_bucket_edges(EDGES)
    for partition in ('dev','test'):
        original=[c for c in calls if c['partition']==partition];reparsed,changes=reparse_calls(original,out)
        write(out/'llm'/f'{partition}-parse-changes.json.gz',changes)
        groups={}
        for label,kb,cc in [('edge_original',edge_a,original),('edge_generated',edge_b,reparsed)]:
            groups[label]=edge_evaluate(kb,cc)
        for label,kb,cc in [('lattice_original',lat_a,original),('lattice_generated',lat_b,reparsed)]:
            cache={};rr=[]
            for c in cc:
                key=fingerprint([c['repo'],c['clauses'],c['parse_reason']])
                if key not in cache:
                    raw=kb.predict_load_samples(c['repo'],c['clauses'],1e12) if not c['parse_reason'] else []
                    cache[key]=estimate_row(compose('lattice',raw,edges,reason=c['parse_reason'],clauses=c['clauses']).targets['duration_ms'])
                rr.append({**{k:c[k] for k in ('id','task','repo','repo_group','actual_ms','actual_bucket')},**cache[key]})
            groups[label]=rr
        affected={r['id'] for r in changes}
        summary[partition]={'changed_clauses':len(changes),'changed_calls':len(affected),'calls':len(original),
                             'all':{n:metrics(rr) for n,rr in groups.items()},
                             'affected':{n:metrics([r for r in rr if r['id'] in affected]) for n,rr in groups.items()}}
        for n,rr in groups.items():write(out/'llm'/f'{partition}-{n}.json.gz',rr)
        progress('llm_evaluated',partition=partition,changed_calls=len(affected))
    write(out/'llm/evaluation.json',summary)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['generate','evaluate']);p.add_argument('--out',type=Path,default=HERE/'results');a=p.parse_args()
    {'generate':generate,'evaluate':evaluate}[a.phase](a.out)
