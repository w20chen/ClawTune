"""Summarize archived held-out predictions and the independently replayed audit."""
from collections import Counter
import gzip
import hashlib
import html
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
RUN = ROOT / '.runtime/offline/kb-methods-20261012'


def read(path):
    data = path.read_bytes()
    return json.loads(gzip.decompress(data) if path.suffix == '.gz' else data)


rows = [json.loads(line) for line in (RUN / 'model-predictions.jsonl').read_bytes().splitlines()]
models = {m: [r for r in rows if r['model'] == m and r['target'] == 'duration_ms']
          for m in ('lattice', 'lattice_loso', 'lattice_max_cardinality')}
base = models['lattice']
available = [r for r in base if r['status'] == 'available']
ranked = sorted(available, key=lambda r: abs(r['actual'] - r['p50']), reverse=True)
evidence = read(OUT / 'current30_evidence.json.gz')
findings = read(ROOT / 'research/lattice_p0_evaluation/current30_findings.json')
examples = read(OUT / 'training_examples.json.gz')
report = read(RUN / 'report.json')
bucket_summary = read(OUT.parent / 'summary.json')
groups = {rank: g['label'] for g in findings['groups'] for rank in g['ranks']}
comparisons = {}
for model, records in models.items():
    assert len(base) == len(records)
    assert all((x['task'], x['tool'], x['actual']) == (y['task'], y['tool'], y['actual'])
               for x, y in zip(base, records, strict=True))
    pairs = [(x,y) for x,y in zip(base,records,strict=True) if x['status'] == 'available']
    assert all(y['status'] == 'available' for x,y in pairs)
    comparisons[model] = {
        'same_p50': sum(math.isclose(x['p50'], y['p50'], abs_tol=1e-6) for x,y in pairs),
        'metrics': next(m for m in report['models'][model]['metrics'] if m['target'] == 'duration_ms'),
        'shell_buckets': bucket_summary['shell_models'][model],
    }

cases = []
for c,r in zip(evidence['calls'],ranked[:30],strict=True):
    assert math.isclose(c['prediction']['p50'],r['p50'],abs_tol=1e-6)
    assert c['actual_ms'] == r['actual']
    matching_indices = [i for i,x in enumerate(base) if x == r]
    assert len(matching_indices) == 1
    index = matching_indices[0]
    variants = {m:rr[index]['p50'] for m,rr in models.items()}
    cases.append(dict(c, absolute_error_ms=abs(r['actual']-r['p50']),
                      actual_bucket=r['actual_bucket'], predicted_bucket=r['predicted_bucket'],
                      bucket_correct=r['actual_bucket']==r['predicted_bucket'],
                      primary_cause=groups[c['rank']], variant_p50_ms=variants))

total_error = sum(abs(r['actual']-r['p50']) for r in available)
summary = {
    'scope': 'duration_ms; shell calls; frozen task holdout; no test updates',
    'ranking': 'descending absolute error of call-level p50, among available labeled predictions',
    'train_tasks': report['train_tasks'], 'test_tasks': report['test_tasks'],
    'test_updates': report['test_updates'], 'split_sha256': report['split_sha256'],
    'comparisons': comparisons,
    'worst30': {
        'count': 30, 'available_fraction': 30/len(available),
        'absolute_error_share': sum(c['absolute_error_ms'] for c in cases)/total_error,
        'underpredicted': sum(c['prediction']['p50']<c['actual_ms'] for c in cases),
        'actual_above_p90': sum(c['prediction']['p90']<c['actual_ms'] for c in cases),
        'bucket_correct': sum(c['bucket_correct'] for c in cases),
        'task_count': len({r['task'] for r in ranked[:30]}),
        'repositories': dict(Counter(r['repo'] for r in ranked[:30])),
        'same_p50_as_max_cardinality': sum(math.isclose(c['prediction']['p50'],c['variant_p50_ms']['lattice_max_cardinality'],abs_tol=1e-6) for c in cases),
        'same_p50_as_loso': sum(math.isclose(c['prediction']['p50'],c['variant_p50_ms']['lattice_loso'],abs_tol=1e-6) for c in cases),
    },
    'cause_groups': findings['groups'],
    'limitations': findings['limitations'] + [
        'Absolute error ranking emphasizes long calls and is not a ranking by bucket error.',
        'Thirty calls include repeated calls within tasks; they are not thirty independent workloads.',
        'No algorithm changes or hyperparameter search were performed in this audit.',
    ],
}
(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
(OUT/'cases.json.gz').write_bytes(gzip.compress(json.dumps(cases,ensure_ascii=False).encode(),mtime=0))

escape = lambda x: html.escape(str(x))
parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Shrinkage 最坏 30 条审计</title>',
         '<style>body{font:15px/1.6 system-ui;max-width:1300px;margin:36px auto;padding:0 24px;color:#202938}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}th{background:#eef2f8}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f6f8;padding:14px}summary{cursor:pointer}article{margin:32px 0;border-top:2px solid #bbc8da}small{color:#536174}</style>',
         '<h1>Shrinkage：当前效果与最坏的 30 次调用</h1>',
         '<p>固定按任务划分：515 个训练任务、116 个测试任务；测试不更新 KB。排序按可预测调用的 |实际耗时 − p50|，单位为秒。已用当前代码复现 30 次预测、核对 18 个节点样本分布、原始测试文件哈希及 23 条训练输出。</p>',
         '<p>Shell 覆盖率 95.14%，分桶准确率 69.99%，含拒答的宏平均召回率 54.53%；MAE 12.55 秒，中位绝对误差 0.170 秒。最坏 30 条占总绝对误差 49.03%，28 条低估；20 条分桶仍正确。30 条均与 max-cardinality 的 p50 相同。</p>',
         '<p>因此，本批严重误差不能单独归因于 shrinkage 选点风险：需要先处理测试工作量泛化、精确特征内的异质历史和条件链合成。以下分类是逐条证据归因，未做优化消融，也不表示单独更改该因素必然改善。</p>',
         '<table><tr><th>排名 / 调用</th><th>实际 s</th><th>p50 s</th><th>绝对误差 s</th><th>分桶</th><th>主要原因</th></tr>']
for c in cases:
    parts.append(f'<tr><td><a href="#case{c["rank"]}">{c["rank"]}. {escape(c["id"])}</a></td><td>{c["actual_ms"]/1000:.3f}</td><td>{c["prediction"]["p50"]/1000:.3f}</td><td>{c["absolute_error_ms"]/1000:.3f}</td><td>{"正确" if c["bucket_correct"] else "错误"}</td><td>{escape(c["primary_cause"])}</td></tr>')
parts.append('</table>')
for c in cases:
    parts.append(f'<article id="case{c["rank"]}"><h2>{c["rank"]}. {escape(c["id"])}</h2><pre>{escape(c["command"])}</pre><p>{escape(c["primary_cause"])}；p90={c["prediction"]["p90"]/1000:.3f} 秒。</p><small>来源：{escape(c["source_file"])}:{c["source_line"]}</small>')
    for d in c['clauses']:
        parts.append('<details><summary>子命令 '+escape(' '.join(d['argv']))+'</summary><pre>'+escape(json.dumps({k:v for k,v in d.items() if k!='node'},ensure_ascii=False,indent=2))+'</pre>')
        if d.get('node'):
            parts.append('<details><summary>所有选中训练样本（按仓库、argv 分组；durations_ms 为毫秒）</summary><pre>'+escape(json.dumps(evidence['nodes'][d['node']],ensure_ascii=False,indent=2))+'</pre></details>')
        parts.append('</details>')
    parts.append('<details><summary>本次执行输出尾部</summary><pre>'+escape(c['output_tail'])+'</pre></details>')
    for x in examples:
        if x['current_rank']==c['rank']:
            parts.append('<details><summary>训练实例 '+escape(x['duration_ms']/1000)+' 秒：'+escape(' '.join(x['argv']))+'</summary><pre>'+escape(json.dumps(x['source'],ensure_ascii=False))+'\n'+escape(x['output_tail'])+'</pre></details>')
    parts.append('</article>')
parts.append('<p>分桶：[0,0.1)、[0.1,0.5)、[0.5,2)、[2,10)、[10,+∞) 秒。预测桶取经验分布的最大概率桶，非 p50 所在桶。精确指解析后的完整特征相同，不保证依赖、环境或仓库版本相同。事后退出状态和实际测试数量仅用于解释，不能作为本次调用预测输入。</p></html>')
(OUT/'report.html').write_text('\n'.join(parts),encoding='utf8')

provenance = read(OUT.parent/'provenance.json')
source_paths = [p for p in provenance['source_sha256'] if p.startswith(('services/sidecar/src/tool_time/', 'services/sidecar/src/command_parser/'))]
current_hashes = {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in source_paths}
assert all(h == provenance['source_sha256'][p] for p,h in current_hashes.items())
(OUT/'verification.json').write_text(json.dumps({
    'replayed_calls':len(cases), 'verified_node_distributions':len(evidence['nodes']),
    'verified_training_outputs':len(examples), 'source_sha256':current_hashes,
    'snapshot_sha256':hashlib.sha256((RUN/'seed/clause-lattice-time-kb.json').read_bytes()).hexdigest(),
    'artifacts_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file() and p.name!='verification.json'},
},indent=2)+'\n',encoding='utf8')
print(json.dumps(summary['worst30'],ensure_ascii=False,indent=2))
