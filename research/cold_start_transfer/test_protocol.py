"""Protocol tests use artificial values only as tests, never study observations."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).parent))
from evaluate import split_prefix, quantiles, empirical, residuals, adjust, choose_smoothing, observation


def record(identity,task,time,tool='pytest',value=1000,repo='target'):
    return dict(id=identity,task=task,repo=repo,tool=tool,call_id=identity,
                observation=dict(repo=repo,bin=tool,argv=[tool,'tests'],ts_start=time,ts_end=time+1,
                                 latency_ms=value,sampled_peak_rss_mb=4,cpu_ns_cumulative=20),
                values={'duration_ms':value})


def test_task_attempts_never_cross_calibration_test():
    rows=[record(str(i),'first',i*2) for i in range(25)]+[record('later','second',100)]
    prefix,test,ns=split_prefix(rows)
    assert ns==[0,1,2,5,10,20]
    assert len(prefix)==20 and [r['id'] for r in test]==['later']
    # Remaining first-task outcomes cannot become test labels after the prefix.
    assert not ({r['task'] for r in prefix}&{r['task'] for r in test})


def test_single_task_supports_only_zero_shot():
    rows=[record(str(i),'same',i*2) for i in range(30)]
    prefix,test,ns=split_prefix(rows)
    assert not prefix and test==rows and ns==[0]


def test_source_predictions_and_family_bias_do_not_use_query_label():
    pools=empirical([record('s','source',0,value=100)],'duration_ms')
    residual=residuals([record('c','calibration',2,value=200)],pools,'duration_ms')
    original=pools[('family','pytest')][0]
    assert adjust(original,residual['pytest'],0,1000)[0] == __import__('pytest').approx(200)
    assert adjust(original,residual['pytest'],5,1000)[0]<200
    assert adjust(original,residual.get('git',[]),0,1000)==original
    assert ('family','git') not in pools
    assert ('executable','git') not in pools


def test_target_budget_does_not_leak_other_resources():
    source=record('id','task',1)
    o=observation(source,'duration_ms',10)
    assert o.sampled_peak_rss_mb is None and o.cpu_ns_cumulative is None
    assert o.latency_ms==1000 and o.ts_end<24
    assert observation(source,'duration_ms',10,True).repo==''


def test_quantiles_use_real_atoms_and_keep_zero():
    assert quantiles([]) is None
    assert quantiles([0,10])==(5,10)
    assert quantiles([0,10],[1,1])==(5,10)
    assert quantiles([1,10,100],[8,1,1])==(1,10)


def test_no_inner_data_is_explicit_not_test_tuned():
    value,details=choose_smoothing([record('s','only',0,repo='source')],'duration_ms')
    assert value==5 and details['status']=='no_inner_few_shot_data_default_5'


def test_new_repository_edge_weights_start_from_prior():
    from edge_kappa_kb import EdgeKappaKB
    from evaluate import event
    from tool_time.edge_kappa_adapter import shell_query
    rows=[record(str(i),'source_task',i*2,value=100*(i+1),repo='source') for i in range(5)]
    learned=EdgeKappaKB.fit([event(row,i) for i,row in enumerate(rows)])
    fixed=EdgeKappaKB.fit([event(row,i) for i,row in enumerate(rows)],{'learn_weights':False})
    query=shell_query('pytest tests',repo='new_repository')
    a,b=learned.predict_duration(query),fixed.predict_duration(query)
    assert not a.bucket_prediction.exact_match
    assert a.values_ms==b.values_ms and a.weights==b.weights


def test_measured_memory_cgroup_must_contain_execution():
    from prepare import memory_scope_reason
    resources=dict(memory_measurement='cgroup_v2_memory_current',memory_environment_id='/sys/fs/cgroup/docker-1')
    assert memory_scope_reason(resources,{'cgroup_path':'/sys/fs/cgroup/clawtune/exec-1'})
    assert memory_scope_reason(resources,{'cgroup_path':'/sys/fs/cgroup/docker-10'})
    assert memory_scope_reason(resources,{'cgroup_path':'/sys/fs/cgroup/docker-1'}) is None
    assert memory_scope_reason(resources,{'cgroup_path':'/sys/fs/cgroup/docker-1/exec-1'}) is None
    assert memory_scope_reason(resources,{'cgroup_path':None}) is None
    resources['memory_measurement']='cgroup_v2_environment_union_v1'
    assert memory_scope_reason(resources,{'cgroup_path':'/sys/fs/cgroup/clawtune/exec-1'}) is None
