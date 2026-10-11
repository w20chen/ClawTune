"""Raw per-metric missingness and execution outcomes, separate from model labels."""
import argparse
import json
from collections import Counter
from pathlib import Path
from prepare import _completed_call_from_tool_span
from cold_start.flat_loader import _censored_call
from evaluate import csv_write, dump


def audit(data,output):
    sources=json.loads((data/'source_manifest.json').read_text(encoding='utf-8'))
    rows=[];reasons=[]
    for source in sources:
        counts=Counter();availability=Counter();starts={}
        with Path(source['source']).open('rb') as stream:
            for raw in stream:
                if source['stratum'].startswith('v5:'):
                    if b'"tool_exec"' not in raw:continue
                    record=json.loads(raw)
                    if record.get('action_type')!='tool_exec':continue
                    values=record.get('data') or {};status=values.get('success')
                    censored=_censored_call(values)
                    clauses=(values.get('resource_observation') or {}).get('clauses') or []
                else:
                    if b'"span_start"' not in raw and b'"span_end"' not in raw:continue
                    record=json.loads(raw)
                    if record.get('kind')!='tool':continue
                    if record.get('record_type')=='span_start':starts[record['span_id']]=record;continue
                    if record.get('record_type')!='span_end':continue
                    status=(record.get('status') or {}).get('code')
                    status=True if status=='ok' else False if status=='error' else None
                    completed=_completed_call_from_tool_span(starts.get(record['span_id']),record,repo=record.get('repo') or source['repo'] or 'unknown')
                    censored=completed.censored if completed is not None else None
                    clauses=(((record.get('execution') or {}).get('tool_resource') or {}).get('call_telemetry') or {}).get('clauses') or []
                    resources=record.get('resources') or {}
                    availability[('tool_environment_memory',str(resources.get('memory_unavailable_reason') or ('eligible' if resources.get('memory_eligible') else 'absent')))]+=1
                counts['tool_records']+=1
                counts['reported_success' if status is True else 'reported_failure' if status is False else 'reported_status_unknown']+=1
                counts['censored' if censored is True else 'censor_status_unknown' if censored is None else 'not_marked_censored']+=1
                counts['raw_clause_records']+=len(clauses)
                counts['tools_without_clause_records']+=not bool(clauses)
                for clause in clauses:
                    a=clause.get('availability') or {}
                    for target in ('latency','memory','cpu_time','cpu'):
                        availability[('clause_'+target,str(a.get(target,'absent')))]+=1
        known=counts['reported_success']+counts['reported_failure']
        rows.append(dict(source=source['source'],sha256=source['sha256'],stratum=source['stratum'],
                         task=source['task'],reported_failure_fraction=counts['reported_failure']/known if known else None,
                         **counts))
        for (metric,reason),n in sorted(availability.items()):
            reasons.append(dict(source=source['source'],stratum=source['stratum'],metric=metric,reason=reason,n=n))
    csv_write(output/'source_execution_quality.csv',rows)
    csv_write(output/'raw_availability_reasons.csv',reasons)
    total=Counter()
    for row in rows:
        for k,v in row.items():
            if isinstance(v,int):total[k]+=v
    dump(output/'execution_quality_totals.json',dict(total))
    print('Raw quality audit:',len(rows),'files',dict(total))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();audit(a.data,a.output)
