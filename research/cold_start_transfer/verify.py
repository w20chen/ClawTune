"""Check an independent full rerun, including every per-observation prediction."""
import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_memory_windows(data, results):
    """Recompute qualified clause peaks from original sampled timelines."""
    rows=[json.loads(line) for line in (data/'observations.jsonl').open(encoding='utf-8')]
    cache={};checked=[]
    for row in rows:
        if 'memory_extra_peak_bytes' not in row['values']:continue
        if row['source'] not in cache:
            cache[row['source']]=Path(row['source']).read_text(encoding='utf-8').splitlines()
        raw=json.loads(cache[row['source']][row['source_line']-1])
        obs=row['observation'];start,end=obs['ts_start'],obs['ts_end']
        points=raw['resources']['memory_timeline']
        before=[p for p in points if p[0]<=start];inside=[p for p in points if start<p[0]<=end]
        assert before and inside, row['id']
        times=[start,*[p[0] for p in inside],end]
        gaps=[b-a for a,b in zip(times,times[1:])]
        assert 0<=start-before[-1][0]<=.15 and all(0<=g<=.15 for g in gaps),row['id']
        assert obs['memory_baseline_bytes']==before[-1][1],row['id']
        assert obs['memory_total_peak_bytes']==max(p[1] for p in inside),row['id']
        assert obs['memory_extra_peak_bytes']==max(0,max(p[1] for p in inside)-before[-1][1]),row['id']
        checked.append(dict(id=row['id'],source=row['source'],source_line=row['source_line'],
                            max_sampling_gap_seconds=max(gaps),baseline_age_seconds=start-before[-1][0]))
    (results/'memory_timeline_verification.json').write_text(json.dumps(dict(checked=len(checked),failures=0,records=checked),indent=2)+'\n',encoding='utf-8')


def verify(first_data,first_results,second):
    second_data=second/'data';second_results=second/'results'
    verify_memory_windows(first_data,first_results)
    comparisons=[]
    for name in ('observations.jsonl','source_manifest.json','data_quality.json','memory_scope_quarantine.json'):
        a,b=first_data/name,second_data/name
        comparisons.append(dict(path='data/'+name,sha256=digest(a),matches=digest(a)==digest(b)))
    first_files=sorted(p.relative_to(first_results) for p in (first_results/'folds').rglob('*.json*'))
    second_files=sorted(p.relative_to(second_results) for p in (second_results/'folds').rglob('*.json*'))
    assert first_files==second_files,'fold file roster mismatch'
    for name in first_files:
        a,b=first_results/name,second_results/name
        comparisons.append(dict(path=name.as_posix(),sha256=digest(a),matches=digest(a)==digest(b)))
    assert {p.name for p in first_results.glob('*.csv')}=={p.name for p in second_results.glob('*.csv')},'CSV roster mismatch'
    for a in sorted(first_results.glob('*.csv')):
        b=second_results/a.name
        comparisons.append(dict(path=a.name,sha256=digest(a),matches=b.exists() and digest(a)==digest(b)))
    result=dict(independent_full_rerun=True,compared_files=len(comparisons),
                exact_matches=sum(x['matches'] for x in comparisons),files=comparisons)
    (first_results/'reproducibility.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    assert all(x['matches'] for x in comparisons),'reproduction mismatch; inspect reproducibility.json'
    print('Exact full-run reproduction:',len(comparisons),'files')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--results',type=Path,required=True)
    p.add_argument('--rerun',type=Path,required=True);a=p.parse_args();verify(a.data,a.results,a.rerun)
