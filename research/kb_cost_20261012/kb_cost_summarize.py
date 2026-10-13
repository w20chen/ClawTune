import gzip, hashlib, html, json, re, shutil, statistics
from pathlib import Path
out=Path('research/kb_cost_20261012')
inputs=json.loads((out/'inputs.json').read_bytes())
families={f:json.loads((out/(f+'.json')).read_bytes()) for f in ('tool','trie','edge','lattice0','lattice6')}
def duration_rows(folder):
    rows=[json.loads(line) for line in gzip.decompress((folder/'model-predictions.jsonl.gz').read_bytes()).splitlines()]
    return {m:[r for r in rows if r['model']==m and r['target']=='duration_ms' and r['tool'] in ('exec','terminal_exec')]
        for m in {r['model'] for r in rows}}
current=duration_rows(Path('research/kb_methods_fixed6_20261012'))
prior=duration_rows(Path('research/kb_methods_evaluation_20261012'))
verified={}; timing_rows=[]; detail_rows=[]; pass_query={}
for family,data in families.items():
    records=[json.loads(line) for line in (out/(family+'-rows.jsonl')).read_bytes().splitlines()]
    for r in records:
        m={'shrinkage':'lattice','loso':'lattice_loso','max_cardinality':'lattice_max_cardinality','edge':'edge_kappa'}.get(r['algorithm'],r['algorithm'])
        expected=(prior if family=='lattice0' else current)[m][inputs['sample_indices'][r['sample']]]
        assert expected['task']==r['task']
        assert expected['actual']==inputs['samples'][r['sample']]['actual_ms']
        assert r['status']==expected['status'],(family,r,expected)
        assert (r['p50']==expected['p50'] or (r['p50'] is not None and expected['p50'] is not None and abs(r['p50']-expected['p50'])<1e-6)),(family,r,expected)
    verified[family]=len(records)
    pass_query[family]={}
    for algorithm,parts in data['query'].items():
        pass_query[family][algorithm]={}
        for label,repeats in [('first_pass',(0,)),('repeated_passes',(1,2))]:
            values=sorted(r['match_ms']+r['compose_ms'] for r in records if r['algorithm']==algorithm and r['repeat'] in repeats)
            import math
            pass_query[family][algorithm][label]=dict(n=len(values),p50_ms=statistics.median(values),
                p95_ms=values[math.ceil(.95*len(values))-1],max_ms=max(values))
        label=family+('/'+algorithm if family.startswith('lattice') else '')
        timing_rows.append([label,*[round(parts['match'][k],3) for k in ('p50_ms','p95_ms')],
            *[round(parts['combined'][k],3) for k in ('p50_ms','p95_ms','max_ms')]])
        detail_rows.append([label,*[round(parts['compose'][k],3) for k in ('p50_ms','p95_ms','max_ms')]])
log=Path('.runtime/kb-methods-fixed6-evaluation-r2.log').read_text()
slow=[float(x) for x in re.findall(r'ORACLE DONE \d+ ([\d.]+)',log)]
summary=dict(families=families,query_table=timing_rows,compose_table=detail_rows,verified_prediction_rows=verified,pass_query=pass_query,
    split_sha256=inputs['split_sha256'],sampling_seed=inputs['sampling_seed'],sample_calls=128,repeats=3,
    sampled_tasks=len({r['task'] for r in inputs['samples']}),
    previous_run=dict(seconds=2326.993578689,slow_oracle_count=len(slow),logged_oracle_seconds_lower_bound=sum(slow),
        slowest_two_oracle_seconds=sum(sorted(slow)[-2:]),
        note='only oracle calls over 3 seconds were logged; mixed acceleration and live validation; not a pure CPU attribution'),
    limits=['clauses and features prepared before timing; outer shared shell parsing and HTTP excluded; any backend-internal parsing or lazy initialization remains timed',
        'call-level composition measured, excludes per-clause diagnostic serialization',
        'single-process per family; no oracle or evaluation-only cache; existing production caches retained',
        'three passes on 128 uniformly sampled labeled shell calls, not a concurrent load test',
        '32 replayed completion events for in-memory update timing; timestamps synthetic, resource labels unchanged',
        'Lattice enqueue is not a completed update; full generation preparation measured separately',
        'snapshot conversion and JSON encoding measured separately; filesystem writes not timed'])
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
def table(headers,rows):
    return '<table><tr>'+''.join('<th>'+html.escape(str(x))+'</th>' for x in headers)+'</tr>'+''.join(
        '<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in r)+'</tr>' for r in rows)+'</table>'
maintenance=[]
for f,d in families.items():
    m=d['maintenance']; v=m.get('observe_and_commit')
    maintenance.append([f,round(d['ready_seconds'],3),round(d['peak_rss_mib'],1),
        '仅入队，另需重建' if v is None else round(v['p50_ms'],3),
        '仅入队，另需重建' if v is None else round(v['p95_ms'],3),
        round(d['snapshot_serialize_ms'],2),round(d['snapshot_json_encode_ms'],2)])
page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>KB 维护与预测成本</title><style>body{font:16px/1.6 system-ui;max-width:1300px;margin:32px auto;padding:0 20px}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #bbb;padding:8px;text-align:right}td:first-child,th:first-child{text-align:left}</style>'
page+='<h1>KB 维护与预测成本</h1><p>固定相同训练快照。均匀抽取 128/1275 条有耗时标签的 shell 调用，覆盖 47 个任务，重复 3 次。Lattice 0 仅为控制实验，使用当前代码但将节点生成边界覆盖为 0；不修改生产配置。各进程顺序执行，不含 oracle、不启用上一轮实验缓存。</p>'
page+='<h2>已解析调用的预测成本（毫秒）</h2>'+table(['方法','证据查询 P50','证据查询 P95','查询+组合 P50','查询+组合 P95','查询+组合最大'],timing_rows)
page+='<p>统计包含首轮与重复轮次。Tool 和 Trie 首轮出现约 0.3 秒峰值。公共外层解析未计时，但各 backend 内部必要解析或延迟初始化仍在测量范围内。下表分开显示两种情况；本次未禁用产品自身缓存。</p>'
page+=table(['方法','首轮 P50','首轮 P95','重复轮 P50','重复轮 P95'],[
    [f+'/'+a,*[round(r[p][k],3) for p in ('first_pass','repeated_passes') for k in ('p50_ms','p95_ms')]]
    for f,algs in pass_query.items() for a,r in algs.items()])
page+='<h2>单独调用组合成本（毫秒）</h2>'+table(['方法','P50','P95','最大'],detail_rows)
page+='<h2>恢复、维护与快照</h2>'+table(['方法','快照恢复至可查询 秒','进程峰值 RSS MiB','单事件入库 P50 毫秒','单事件入库 P95 毫秒','转快照对象 毫秒','JSON 编码 毫秒'],maintenance)
page+='<p>Edge 的动作前登记及预测另计，详见 summary.json 的 begin_including_prediction。Lattice 的后台完整重建是实际维护成本，不能以入队耗时代替；在线 fork + pending 的 prepare 路径可能分别构建原有和含新增样本的两份统计，本实验没有把这种在线双重构建计时为已实测。</p>'
page+='<p>恢复时间从读取现有 JSON 快照开始，包含解析、恢复和 prepare，不是从原始训练数据首次训练的时间。峰值 RSS 是独立进程全程峰值，包括解释器、快照解码和建库临时对象，不等于常驻 KB 净大小。单事件更新只有 32 个回放样本，Tool/Trie 也观察到约 161/247 毫秒的最大值；不是持久化完成时延。</p>'
page+='<h2>范围限制</h2><ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in summary['limits'])+'</ul><p>全部抽样预测与对应原离线结果核对通过。源码哈希、运行命令和原始逐次耗时保存在本目录。</p></html>'
(out/'report.html').write_text(page,encoding='utf8')
for name in ('kb_cost_prepare.py','kb_cost_worker.py','kb_cost_summarize.py'): shutil.copyfile(Path('.runtime')/name,out/name)
provenance=json.loads(Path('research/kb_methods_fixed6_20261012/provenance.json').read_bytes())
for name,digest in provenance['source_sha256'].items(): assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest,name
manifest=dict(python='WSL Python 3.14.4',source_sha256=provenance['source_sha256'],
    run_order=['lattice6','lattice0','tool','trie','edge'],
    command='PYTHONPATH=services/sidecar/src:. PYTHONHASHSEED=0 python .runtime/kb_cost_worker.py <family>',
    artifact_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file() and p.name!='provenance.json'})
(out/'provenance.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('QUERY',json.dumps(timing_rows)); print('COMPOSE',json.dumps(detail_rows)); print('MAINTENANCE',json.dumps(maintenance)); print('VERIFIED',verified)
