"""Prepare a reproducible call sample; source traces are read-only."""
import json, random, time
from pathlib import Path
from dataclasses import asdict
from offline.runner import load_task, _call_actuals, _safe_recorded_clause
from clawtune_sidecar.predictors.call_load import plain_execution
from command_parser import default_service, ParseContext
from tool_resource.commands import unwrap_argv
from tool_resource.features import shell_bin_requires_exec_evidence
from tool_resource.runtime_kb import is_pipeline_dependent_consumer

out=Path('research/kb_cost_20261012'); out.mkdir(exist_ok=True)
split=json.loads(Path('.runtime/offline/kb-methods-fixed6-20261012-r2/split.json').read_bytes())
dataset=Path(split['current_source_path'])
pool=[]; update_calls=[]; update_clauses=[]
for ti,key in enumerate(split['test']):
    loaded=load_task(dataset,split['tasks'][key],'MiB')
    for row in loaded.clauses:
        if (row.latency_ms is not None and row.latency_ms>=0 and not row.in_loop and not row.in_subst
            and not is_pipeline_dependent_consumer(row) and len(update_clauses)<32):
            update_clauses.append(asdict(row))
    for i,call in enumerate(loaded.calls):
        if call.censored: continue
        actuals=_call_actuals(call)
        if actuals and len(update_calls)<32: update_calls.append(asdict(call))
        if i<len(loaded.call_actuals): actuals.update(loaded.call_actuals[i])
        if call.tool_name not in ('exec','terminal_exec') or 'duration_ms' not in actuals: continue
        pool.append(dict(task=key,index=i,repo=call.repo,tool=call.tool_name,command=call.command,
            recorded=loaded.call_clauses[i] if i<len(loaded.call_clauses) else (),actual_ms=actuals['duration_ms']))
    if ti%25==0: print('PREP',ti,flush=True)
assert len(pool)==1275,len(pool)
selected=sorted(random.Random(20261012).sample(range(len(pool)),128))
samples=[]
for index in selected:
    row=pool[index]
    known=_safe_recorded_clause(row['command'],row['recorded'])
    if known is not None:
        clauses=[]
        for ci,c in enumerate(known):
            argv,env,assumptions,why=unwrap_argv(c.get('argv',[]))
            if not argv or not shell_bin_requires_exec_evidence(argv[0].rsplit('/',1)[-1],argv[0]): continue
            clauses.append(dict(c,syntax_argv=c.get('syntax_argv',c['argv']),argv=argv,bin=argv[0].rsplit('/',1)[-1],
                clause_index=c.get('clause_index',ci),env=env,prediction_assumptions=assumptions,prediction_unavailable_reason=why))
        reason=None
    else:
        clauses,reason=plain_execution(row['command'])
    if not reason:
        clauses=default_service().prepare(clauses,ParseContext(repo=row['repo']))
    samples.append({**{k:v for k,v in row.items() if k!='recorded'},'clauses':clauses,'reason':reason})
payload=dict(split_sha256=split['assignment_sha256'],sampling_seed=20261012,pool_count=len(pool),sample_indices=selected,
    samples=samples,update_calls=update_calls,update_clauses=update_clauses)
(out/'inputs.json').write_text(json.dumps(payload,ensure_ascii=False)+'\n',encoding='utf8')
print('READY',len(samples),len(update_calls),len(update_clauses),flush=True)
