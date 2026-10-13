"""Archive and independently compare the completed seven-method evaluation."""
import gzip
import hashlib
import html
import json
from pathlib import Path
import statistics

from tools.summarize_offline_buckets import compare, render
from offline.runner import _metric_summary

root=Path.cwd()
run=root/'.runtime/offline/kb-methods-fixed6-20261012-r2'
out=root/'research/kb_methods_fixed6_20261012'
report=json.loads((run/'report.json').read_bytes())
split=json.loads((run/'split.json').read_bytes())
old=root/'research/kb_methods_evaluation_20261012'
previous=json.loads((old/'summary.json').read_bytes())
assert report['split_sha256']==previous['split_sha256']
assert report['train_tasks']==515 and report['test_tasks']==116 and report['test_updates']==0
summary=compare(run)
assert len(summary['models'])==7
rows=[json.loads(line) for line in (run/'model-predictions.jsonl').read_bytes().splitlines()]
grouped={m:[r for r in rows if r['model']==m and r['target']=='duration_ms'] for m in summary['models']}
identity=lambda r:(r['task'],r['repo'],r['tool'],r['actual'])
for name,model_rows in grouped.items():
    assert [identity(r) for r in model_rows]==[identity(r) for r in grouped['lattice']]
oracle=grouped['lattice_oracle']
paired={}
for model in ('lattice','lattice_loso','lattice_max_cardinality'):
    pairs=[(r,o) for r,o in zip(grouped[model],oracle,strict=True) if r['status']==o['status']=='available']
    assert all(abs(o['p50']-o['actual'])<=abs(r['p50']-r['actual'])+1e-6 for r,o in pairs),model
    paired[model]=dict(count=len(pairs),model_mae_ms=statistics.mean(abs(r['p50']-r['actual']) for r,o in pairs),
        oracle_mae_ms=statistics.mean(abs(o['p50']-o['actual']) for r,o in pairs),
        improved=sum(abs(o['p50']-o['actual'])<abs(r['p50']-r['actual'])-1e-6 for r,o in pairs))
summary['oracle_comparison']=paired
summary['oracle_definition']=report['models']['lattice_oracle']['oracle']
summary['duration_metrics']={m:next(x for x in v['metrics'] if x['target']=='duration_ms') for m,v in report['models'].items()}
summary['shell_duration_metrics']={m:_metric_summary('swe-rebench','duration_ms',
    [r for r in v if r['tool'] in ('exec','terminal_exec')],report['bucket_edges']['duration_ms']) for m,v in grouped.items()}
summary['common_duration_mae_ms']={m:statistics.mean(abs(r['p50']-r['actual']) for i,r in enumerate(v)
    if all(rr[i]['status']=='available' for rr in grouped.values())) for m,v in grouped.items()}
prior_rows=[json.loads(line) for line in gzip.decompress((old/'model-predictions.jsonl.gz').read_bytes()).splitlines()]
summary['unchanged_models']={}
check_fields=('task','actual','p50','p90','status','predicted_bucket','bucket_probabilities')
for model in ('trie','edge_kappa','tool'):
    prior=([json.loads(line) for line in gzip.decompress((old/'tool_prefix_fix/predictions.jsonl.gz').read_bytes()).splitlines()]
           if model=='tool' else [r for r in prior_rows if r['model']==model and r['target']=='duration_ms'])
    assert len(prior)==len(grouped[model])
    changed=sum(any(a.get(k)!=b.get(k) for k in check_fields) for a,b in zip(prior,grouped[model],strict=True))
    summary['unchanged_models'][model]=dict(compared=len(prior),changed=changed)
summary['previous_lattice_shell']={m:previous['shell_models'][m] for m in ('lattice','lattice_loso','lattice_max_cardinality')}

provenance=json.loads((out/'provenance.json').read_bytes())
for name,checksum in provenance['source_sha256'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==checksum,name
native=provenance.get('native_oracle_acceleration')
if native:
    accelerator_dir=out/'evaluation_acceleration'
    accelerator_dir.mkdir(exist_ok=True)
    for name,checksum in native['files_sha256'].items():
        data=(root/'.runtime'/name).read_bytes()
        assert hashlib.sha256(data).hexdigest()==checksum,name
        (accelerator_dir/name).write_bytes(data)
    assert native['validation']['bit_exact_cases']==1000
    assert native['live_validation']['checked']>=192
verified=0
for task in split['tasks'].values():
    for source in task['files']:
        path=Path(split['current_source_path'])/source['path']
        with path.open('rb') as stream:
            checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        assert checksum==source['sha256'],source['path']
        verified+=1
provenance['verified_source_files_after_run']=verified
provenance['verification']=['same held-out split as preceding evaluation','source hashes unchanged throughout evaluation',
    'all source trace hashes verified after evaluation','bucket summary agrees with functional offline report',
    'oracle p50 error never exceeds any Lattice variant on jointly available calls',
    'all model rows aligned by task, tool and actual duration']
(out/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf8')
for name in ('report.json','split.json','model-predictions.jsonl'):
    (out/(name+'.gz')).write_bytes(gzip.compress((run/name).read_bytes(),mtime=0))
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

def table(headers,rows):
    return '<table><tr>'+''.join('<th>'+html.escape(str(x))+'</th>' for x in headers)+'</tr>'+''.join(
        '<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)+'</table>'

headline='<p><strong>固定 6 个可选特征；oracle 使用测试耗时选择最优调用 p50，仅作事后参照。</strong>测试任务和训练标签与上一轮一致。所有模型独立评分，缺失预测计入全量召回率。</p>'
continuous=table(['方法','MAE 秒（各自可预测）','中位绝对误差 秒','P90 绝对误差 秒','p90 覆盖率','共同集合 MAE 秒'],[
    [m,f"{x['mae']/1000:.3f}",f"{x['median_absolute_error']/1000:.3f}",f"{x['p90_absolute_error']/1000:.3f}",
     f"{100*x['p90_coverage']:.2f}%",f"{summary['common_duration_mae_ms'][m]/1000:.3f}"]
    for m,x in summary['shell_duration_metrics'].items()])
comparison=table(['Lattice 变体','共同可预测调用','变体 MAE 秒','Oracle MAE 秒','Oracle 更优调用'],[
    [m,x['count'],f"{x['model_mae_ms']/1000:.3f}",f"{x['oracle_mae_ms']/1000:.3f}",x['improved']] for m,x in paired.items()])
page=render(summary).replace('<h1>KB 离线耗时分桶评估</h1>','<h1>固定 6：七种 KB 方法离线评估</h1>'+headline)
page=page.replace('</html>','<h2>连续耗时误差</h2><p>下表只统计 shell 调用，同时列出所有方法共同可预测集合的 MAE。各自可预测集合不同，需结合覆盖率解读。</p>'+continuous+
    '<h2>Oracle 与各 Lattice 变体配对</h2><p>Oracle 优化 p50 绝对误差，不直接优化分桶准确率。Shrinkage 仍保留 512 候选评分保护，因此需核对覆盖率。</p>'+comparison+'</html>')
(out/'report.html').write_text(page,encoding='utf8')
if native:
    page=page.replace('</html>', '<p>本轮评测缓存冻结知识库的相同候选查询，并使用经过精确一致性核对的 oracle 内部评分加速；搜索空间、搜索顺序和最终调用组合器保持不变。运行耗时不用于比较各 KB 的在线性能。加速源码、验证数量及哈希记录见 provenance.json。</p></html>')
    (out/'report.html').write_text(page,encoding='utf8')
print('VERIFIED',verified,'files')
print(json.dumps({m:{k:v for k,v in r.items() if k not in ('per_bucket','confusion_matrix_with_abstention_last')} for m,r in summary['shell_models'].items()},indent=2))
print('PAIRED',json.dumps(paired,indent=2))
print('UNCHANGED',summary['unchanged_models'])
