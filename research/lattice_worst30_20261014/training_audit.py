"""Join selected training durations back to their read-only output evidence."""
import gzip
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT/'services/sidecar/src'))
from execution_context.history import failure_label, exit_label

def read(name):
    p = OUT/name
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix=='.gz' else p.read_bytes())

def main():
    rows = read('worst30-evidence.json.gz')
    split = read('split.json')
    dataset = Path('C:/Users/29068/Desktop/swe-rebench-original-flat-644-20260904/swe-rebench-original-flat-644-20260904')
    wanted = set()
    for r in rows:
        for c in r['clause_evidence']:
            for s in c['training_sources']:
                wanted.update((s['task'], tuple(s['argv']), round(v,5)) for v in s['values_ms'])
    targets = {k[0] for k in wanted}
    found = defaultdict(list)
    for task in sorted(targets):
        seen = set()
        for f in split['tasks'][task]['files']:
            if f['sha256'] in seen:
                continue
            seen.add(f['sha256'])
            for line, text in enumerate((dataset/f['path']).read_text(encoding='utf-8-sig').splitlines(), 1):
                if '"tool_exec"' not in text:
                    continue
                row = json.loads(text)
                if row.get('type')!='action' or row.get('action_type')!='tool_exec':
                    continue
                d = row['data']
                output = str(d.get('tool_result',''))
                for c in (d.get('resource_observation') or {}).get('clauses',[]):
                    val = c.get('latency_ms')
                    if not isinstance(val,(int,float)) or not math.isfinite(val):
                        continue
                    key = (task,tuple(c.get('argv',[])),round(val,5))
                    if key not in wanted:
                        continue
                    code = exit_label(output)
                    early = failure_label(output) if code not in (None,0) else None
                    summaries = [l for l in output.splitlines() if re.search(r'\d+ (?:passed|failed|errors?|skipped).*\bin |^Ran \d+ tests? in|^collected \d+|No module named|ERROR collecting|ImportError while loading|ImportError:|Error:|Exit code:', l)]
                    found[key].append({'task':task,'file':f['path'],'line':line,'action_id':row['action_id'],
                        'argv':c['argv'],'duration_ms':val,'exit':code,'early_failure':early,
                        'test_summary':summaries[-8:],'output_tail':output[-1100:]})
    result=[]
    for r in rows:
        for ci,c in enumerate(r['clause_evidence']):
            samples=[]
            for s in c['training_sources']:
                for v in s['values_ms']:
                    matches=found.get((s['task'],tuple(s['argv']),round(v,5)),[])
                    samples.append({'task':s['task'],'argv':s['argv'],'duration_ms':v,'raw_matches':matches})
            result.append({'rank':r['rank'],'id':r['id'],'clause':ci,'exact':c['exact'],'uses_own':c['uses_own'],
                'n':len(samples),'missing_source':sum(not s['raw_matches'] for s in samples),
                'early_failure_counts':dict(Counter(s['raw_matches'][0]['early_failure'] for s in samples if s['raw_matches'])),
                'under_10s':sum(s['duration_ms']<10000 for s in samples),'samples':samples})
    (OUT/'training-output-audit.json.gz').write_bytes(gzip.compress(json.dumps(result,ensure_ascii=False).encode(),mtime=0))
    print(json.dumps([{k:v for k,v in r.items() if k!='samples'} for r in result],ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
