"""Exploratory common CPU-interval study; legacy cgroup ownership is unproven.

Run with --swe PATH --terminal PATH --output PATH. No source data is modified.
Results describe monitored intervals, not proven exclusive tool footprints.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import shlex
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/sidecar/src'))
from cold_start.flat_loader import _censored_call


def valid(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


RULES = {
    'cpu_work_1s': lambda r: r['cpu'] >= 1,
    'cpu_busy_1s': lambda r: r['wall'] >= 1 and r['cores'] >= .8,
    'cpu_busy_2s': lambda r: r['wall'] >= 2 and r['cores'] >= .8,
    'cpu_parallel_1s': lambda r: r['wall'] >= 1 and r['cores'] >= 2,
}


def scan(root, benchmark):
    counts = Counter(); rows = []; rss = []; keys = Counter(); quotas = Counter()
    manifest = [json.loads(l) for l in (root / 'MANIFEST.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    for entry in manifest:
        path = root / entry['flattened_name']
        task = entry.get('instance_id') or entry.get('task_id')
        seen = set(); found = False
        for line in path.open('rb'):
            if b'"tool_exec"' not in line: continue
            r = json.loads(line)
            if r.get('type') != 'action' or r.get('action_type') != 'tool_exec': continue
            d = r['data']; counts['all_tools'] += 1; keys.update(d.keys())
            identity = r.get('action_id')
            if identity in seen: counts['duplicate_actions'] += 1; continue
            seen.add(identity)
            if _censored_call(d): counts['structured_censored'] += 1; continue
            ro = d.get('resource_observation') or {}
            if ro: counts['resource_observation_calls'] += 1
            if ro.get('eligible_for_kb') is True and ro.get('telemetry_quality') == 'ok':
                for c in ro.get('clauses', []):
                    if c.get('eligible_for_kb') is True and c.get('telemetry_quality') == 'ok' and (c.get('availability') or {}).get('memory') == 'ok' and valid(c.get('sampled_peak_rss_mb')):
                        rss.append(c['sampled_peak_rss_mb'])
            tl = d.get('resource_timeline') or {}
            if not tl: continue
            counts['timeline_calls'] += 1
            s = tl.get('summary') or {}; cpu = s.get('cpu_core_s'); wall = s.get('wall_s')
            if tl.get('source') != 'cgroup_cpu_proc_net' or tl.get('scope') != 'openclaw_exec_tool_interval' or not valid(cpu) or not valid(wall) or wall <= 0:
                counts['invalid_timeline'] += 1; continue
            args = d.get('tool_args')
            if isinstance(args, str):
                try: args = json.loads(args)
                except ValueError: args = {}
            args = args if isinstance(args, dict) else {}
            command = args.get('command') or args.get('cmd') or ''
            if not isinstance(command, str): command = ''
            # Treat the entire requested token sequence as a context; never
            # infer executed clauses or branch outcomes from post-run telemetry.
            try: tokens = tuple(shlex.split(command))
            except ValueError: tokens = ()
            samples = tl.get('samples') or []
            for sample in samples:
                if valid(sample.get('cpu_quota_cores')): quotas[str(sample['cpu_quota_cores'])] += 1
            rows.append(dict(task=task, cpu=cpu, wall=wall, cores=cpu/wall,
                             tokens=tokens, tool=d.get('tool_name'), samples=len(samples)))
            found = True
        counts['files'] += 1; counts['files_with_cpu'] += found
    return rows, dict(counts=counts, action_keys=dict(keys), quota_sample_counts=dict(quotas),
        rss_clause_count=len(rss), rss_thresholds={str(t):sum(x>=t for x in rss) for t in (128,256,512,1024)})


def metrics(pairs):
    tn=fp=fn=tp=0
    for a,p in pairs:
        if a and p: tp+=1
        elif a: fn+=1
        elif p: fp+=1
        else: tn+=1
    div=lambda a,b:a/b if b else 0
    return dict(n=len(pairs), tn=tn,fp=fp,fn=fn,tp=tp,
        precision=div(tp,tp+fp),recall=div(tp,tp+fn),f1=div(2*tp,2*tp+fp+fn),
        balanced_accuracy=(div(tp,tp+fn)+div(tn,tn+fp))/2,
        accuracy=div(tp+tn,len(pairs)),all_light_accuracy=div(tn+fp,len(pairs)))


def context_keys(row):
    tokens=row['tokens']; tool=row['tool']
    if not tokens:return [('tool',tool)]
    return [('exact',tool,tokens)]+[('prefix',tool,tokens[:n]) for n in range(min(4,len(tokens)),0,-1)]


def evaluate(rows, seed, cutoff=.5):
    tasks=sorted({r['task'] for r in rows},key=lambda t:hashlib.sha256(f'{seed}:{t}'.encode()).hexdigest())
    train=set(tasks[:int(.8*len(tasks))]); test=[r for r in rows if r['task'] not in train]
    index=defaultdict(list)
    for r in rows:
        if r['task'] in train:
            for key in set(context_keys(r)):index[key].append(r)
    out={}
    for name,rule in RULES.items():
        pairs=[]; covered_cpu=positive_cpu=caught_cpu=0
        for r in test:
            # At least five historical calls from at least two other tasks.
            evidence=next((index[k] for k in context_keys(r) if len(index[k])>=5 and len({x['task'] for x in index[k]})>=2), [])
            if not evidence:continue
            predicted=sum(rule(x) for x in evidence)/len(evidence)>=cutoff
            actual=rule(r);pairs.append((actual,predicted));covered_cpu+=r['cpu']
            if actual:
                positive_cpu+=r['cpu']
                if predicted:caught_cpu+=r['cpu']
        total_heavy_cpu=sum(r['cpu'] for r in test if rule(r))
        total_cpu=sum(r['cpu'] for r in test)
        out[name]={**metrics(pairs),'test_calls':len(test),'coverage':len(pairs)/len(test) if test else 0,
            'decision_probability_cutoff':cutoff,
            'cpu_work_coverage':covered_cpu/total_cpu if total_cpu else None,
            'heavy_cpu_work_recall':caught_cpu/positive_cpu if positive_cpu else None,
            'heavy_cpu_work_recall_all_test':caught_cpu/total_heavy_cpu if total_heavy_cpu else None}
    return out


def render_report(result):
    lines=['# Cross-benchmark heavy-resource study', '',
        'Exploratory legacy cgroup interval diagnostics. CPU is accumulated core-seconds; average cores = CPU / monitored wall time.',
        'CPU-busy candidate: monitored wall >= 1 second AND average cores >= 0.8. Multi-core candidate: wall >= 1 second AND average cores >= 2.',
        '', '| Dataset | All tools | CPU intervals | Busy intervals | Busy fraction | Observed CPU-work share |',
        '|---|---:|---:|---:|---:|---:|']
    for name,d in result['benchmarks'].items():
        r=d['rules']['cpu_busy_1s']
        lines.append(f"| {name} | {d['audit']['counts']['all_tools']} | {r['labeled']} | {r['heavy']} | {r['fraction']:.1%} | {r['cpu_work_share']:.1%} |")
    lines += ['', '## Exploratory KB results', '',
        'Mean over five task-held-out 80/20 splits (17, 42, 73, 101, 137), trained separately per benchmark. Exact requested command tokens, then prefixes up to depth four. Requires five samples from two training tasks. These are a research baseline, not the production KB or an untouched acceptance set.',
        'History stores paired CPU/wall observations; the heavy probability is their empirical heavy fraction. The two probability cutoffs are 0.5 and 0.2. The latter corresponds to a hypothetical false-negative cost four times false-positive cost, not a measured scheduler cost.',
        '', '| Dataset | Cutoff | Coverage | Precision | Recall | F1 | Balanced accuracy | Heavy CPU-work recall, all test |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,d in result['benchmarks'].items():
        for policy,cutoff in [('exploratory_task_holdouts',.5),('recall_priority_holdouts',.2)]:
            m=[s['cpu_busy_1s'] for s in d[policy].values()]
            vals=[statistics.mean(x[k] for x in m) for k in ('coverage','precision','recall','f1','balanced_accuracy','heavy_cpu_work_recall_all_test')]
            lines.append(f'| {name} | {cutoff} | '+' | '.join(f'{v:.1%}' for v in vals)+' |')
    lines += ['', '## Limits and acceptance design', '',
        '- Legacy timeline ownership is unproven: the current production importer withholds these call CPU labels by default. No claim of exclusive tool CPU or measured scheduling benefit is made.',
        '- Counts use canonical tool actions. Missing CPU values are unknown. Fractions over all tools are lower bounds on observed flagged intervals, not measured population prevalence.',
        '- SWE mixes 8-core and 28-core quotas; Terminal uses 8 cores. These are not matched-hardware workload comparisons.',
        '- Structured censor flags are checked, but legacy traces may lack complete timeout/background-process lifecycle records. CPU-work sums can include shared or overlapping activity.',
        '- Terminal has no resource_observation or RSS field in any tool action. Its memory-heavy prevalence and memory prediction quality are unavailable, not zero.',
        '- SWE RSS clause thresholds cannot be compared directly with call-level CPU percentages or environment memory. The source RSS unit must be confirmed before translating thresholds to MiB.',
        '- Peak RSS does not establish sustained memory occupancy, memory bandwidth pressure, or memory-bound execution.',
        '- Proposed memory-capacity rule for fresh collection: additional environment memory >= 256 MiB for >= 1 second, reported alongside a 512 MiB severe tier. These are engineering candidates, not validated common thresholds.',
        '- For acceptance, freeze rules and probability policy before new tasks. Report per-benchmark prevalence, coverage, precision/recall/F1, balanced accuracy, and resource-weighted recall including unknowns.',
        '- Compare default scheduling, measured-oracle scheduling, and predicted scheduling at identical quotas with concurrency 1/2/4/8/16. Measure completed tasks/hour, tool p95/p99 latency, CPU throttling/pressure, memory peak/OOM, and task outcome quality. Oracle improvement must precede any claim of achievable model-driven improvement.',
        '- Maintain CPU-busy and memory-capacity flags independently; a call may satisfy both. Deployment placement remains advisory.', '']
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser();p.add_argument('--swe',type=Path,required=True);p.add_argument('--terminal',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result={'scope':'Exploratory cgroup interval diagnostics; exclusive call ownership and contention benefit unproven', 'benchmarks':{}}
    for name,root in [('swe',a.swe),('terminal',a.terminal)]:
        rows,audit=scan(root,name);total=sum(r['cpu'] for r in rows)
        distribution={}
        for key in ('cpu','wall','cores'):
            values=sorted(r[key] for r in rows)
            distribution[key]={str(q):values[math.ceil(q*len(values))-1] for q in (.5,.75,.9,.95)} if values else {}
        rules={}
        for label,rule in RULES.items():
            heavy=[r for r in rows if rule(r)]
            rules[label]=dict(heavy=len(heavy),labeled=len(rows),fraction=len(heavy)/len(rows) if rows else 0,
                fraction_all_tools_lower_bound=len(heavy)/audit['counts']['all_tools'],
                cpu_work_share=sum(r['cpu'] for r in heavy)/total if total else 0,
                heavy_median_wall=statistics.median(r['wall'] for r in heavy) if heavy else None)
        result['benchmarks'][name]=dict(audit=audit,distribution=distribution,rules=rules,
            exploratory_task_holdouts={str(seed):evaluate(rows,seed) for seed in (17,42,73,101,137)},
            recall_priority_holdouts={str(seed):evaluate(rows,seed,.2) for seed in (17,42,73,101,137)})
        print(name,json.dumps(dict(audit=audit,rules=rules)),flush=True)
    a.output.mkdir(parents=True,exist_ok=True)
    (a.output/'study.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    (a.output/'report.md').write_text(render_report(result),encoding='utf-8')


if __name__=='__main__':main()
