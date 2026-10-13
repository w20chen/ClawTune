"""Export a small resource-rich/causal corpus under either source tree."""
import dataclasses
import json
import random
import sys
from pathlib import Path

from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB


def state(kb):
    def nodes(rows):
        return [{**dataclasses.asdict(node), 'features': sorted(fs)} for fs, node in rows.items()]
    kb._ensure_nodes()
    return dict(legacy=nodes(kb._nodes), globals=[kb._global_log_var,kb._global_log_std,kb._global_median_s],
                resources={key:dict(nodes=nodes(value.nodes), var=value.global_log_var,std=value.global_log_std)
                           for key,value in kb._resource_states.items()})


rng=random.Random(19)
rows=[]
for i in range(40):
    argv=rng.choice([('python','-m','pytest','-q'),('python','-m','pytest','-q','test_a.py'),
                     ('python','work.py'),('python','work.py','--verbose'),('git','status')])
    baseline=100 if i%3 else 0
    total=baseline+i*10
    rows.append(ClauseObservation('r'+str(i%2),argv[0],argv,i,i+.5,
        latency_ms=rng.choice([None,0.,1.,1000.,1000.,1e6]),
        cpu_ns_cumulative=rng.choice([None,0.,1e9,2e9]),cpu_peak_cores=rng.choice([None,0.,1.,2.]),
        sampled_peak_rss_mb=rng.choice([None,0.,10.]),
        memory_baseline_bytes=baseline,memory_total_peak_bytes=total,memory_extra_peak_bytes=total-baseline,
        memory_environment_id='env',memory_eligible=i%5!=0,
        memory_measurement=['cgroup_v2_environment_union_v1','guest_memtotal_minus_memavailable'][i%2],
        in_loop=i%11==0,in_subst=i%13==0))

results={}
for subset in [False,True]:
    kb=LatticeTimeKB.fit(rows,subset_coverage=subset)
    kb.prepare()
    results[str(subset)]=dict(initial=state(kb))
    successor=kb.fork_for_update()
    for i in range(3):
        successor.observe_completed_clause(dataclasses.replace(rows[i+1],ts_start=50+i,ts_end=50.5+i))
    successor.prepare()
    successor.prepare()
    for ts in [50.5,51,52,54]:
        successor._advance(ts)
        results[str(subset)][str(ts)]=state(successor)
    assert state(kb)==results[str(subset)]['initial']
Path(sys.argv[1]).write_text(json.dumps(results,sort_keys=True,separators=(',',':')))
print('EXPORTED',sys.argv[1])
