"""Post-lock evidence review; never feeds test outcomes back into models."""
from collections import Counter, defaultdict
from itertools import combinations
import gzip
import json
import math
from pathlib import Path
import re
import statistics

from research.kb_systematic_20261013.study import HERE, read, write, digest
from research.kb_systematic_20261013.optimization import feature_key

BRANCH = {
    'sqlglot-3630:36': 'venv 创建失败：ensurepip 不可用；实际只执行首阶段，后续 pip/make 未执行。',
    'pennylane-6939:58': 'black --check 报告 4 个文件需重排并退出；后续 pytest 未执行。',
    'pennylane-5063:63': 'pip uninstall 被 externally-managed-environment 拒绝；后续两段 pytest 未执行。',
    'sqlglot-3290:30': '首段 Presto 测试失败；&& 后的全量测试未执行。',
}
FAILURE = {
    'pennylane-2964:66': 'conftest 导入失败，缺少 pkg_resources。',
    'pennylane-1072:31': 'conftest 导入失败，缺少 pkg_resources。',
    'pennylane-1325:40': 'conftest 导入失败，缺少 pkg_resources。',
    'pennylane-2601:24': 'autograd 导入 inspect.getargspec 失败，依赖与解释器不兼容。',
    'pennylane-3381:44': 'autograd 导入 inspect.getargspec 失败，依赖与解释器不兼容。',
    'sqlglot-3290:24': 'pytest 在收集阶段因 5 个 collection errors 中止。',
    'sqlglot-4430:28': 'pytest 收集阶段缺少 pandas，提前中止。',
}
FALLBACK = {
    'pennylane-3483:54': '未见 tests/returntypes 与插件组合；回退丢失路径/插件，实际输出 4139 passed、1119 skipped。',
    'pennylane-3579:45': '三目录组合未命中；回退丢失测试路径，实际接近 598 秒。',
    'pennylane-4161:69': '--lf/-x/-vv/--tb=long 组合未命中；last-failed 的运行集合还依赖未观测缓存。',
    'pennylane-3921:53': '未展开的 glob 使参数保守保留为 opaque；回退丢失测试范围，实际 8814 passed、3140 skipped。',
    'pennylane-3182:39': 'pennylane/devices/tests 范围未命中；回退到粗粒度样本，实际 722 passed、756 failed。',
}

def match(mapping, ident):
    return next((v for k, v in mapping.items() if ident.endswith(k)), None)

def annotate(out):
    from command_parser import default_service, ParseContext
    calls = {c['id']: c for c in read(out/'calls.json.gz')}
    counts = {}; unique = {}; appendix = ['# 最坏案例逐条审计', '',
        '按 abs(log1p(预测秒数) − log1p(实际秒数)) 降序。分类是直接证据支持的主因，允许次因；不是全数据错误比例。', '',
        '每个子句的完整合法候选、历史原始耗时、特征、父边及权重见同名 `results/*-worst30.json.gz`。下列输出片段只用于事后归因，没有输入优化模型。', '']
    for model in ('lattice_shrinkage', 'edge_native'):
        path = out/(model+'-worst30.json.gz'); rows = read(path); appendix += ['## '+model, '']
        for r in rows:
            c = calls[r['id']]
            r['partition'] = c['partition']
            for d, cl in zip(r['clauses'], c['clauses']):
                d['parsed'] = default_service().parser.parse(cl.get('syntax_argv', cl['argv']),
                    ParseContext(repo=c['repo']), word_intents=cl.get('word_intents')).to_dict()
                vals = d['lattice']['selected_values_ms']
                d['lattice']['selected_summary'] = {'n': len(vals), 'min_ms': min(vals) if vals else None,
                    'median_ms': statistics.median(vals) if vals else None, 'max_ms': max(vals) if vals else None}
            direct = match(BRANCH, r['id'])
            if direct:
                category, root = 'B', 'conditional_stage_composition'
                repair = '研究执行成功概率与条件路径合成；不能事后用真实退出状态选择预测分支。'
                secondary = ['D: 前序成功与否未观测', 'C: 部分子句规则/证据不足，但不足以解释未执行阶段被相加']
            elif match(FAILURE, r['id']):
                direct = match(FAILURE, r['id']); category, root = 'D', 'dependency_or_collection_failure'
                repair = '在预测前获取低成本依赖/解释器与可运行状态特征；区分启动失败与正常工作量分布。'
                secondary = ['B: 当前表示未区分运行环境/失败机制；不能由耗时标签反推可用前验']
            elif match(FALLBACK, r['id']):
                direct = match(FALLBACK, r['id']); category, root = 'C', 'unseen_scope_fallback'
                repair = '保留范围/模式语义并学习可迁移的工作量表示；补充规则本身未必得到范围大小。'
                secondary = ['B: 粗粒度证据混合', 'D: 输入规模/缓存未充分观测；具体因果贡献未分离']
            else:
                assert 'sqlglot' in r['id'], r['id']
                direct = '真实输出显示长时间测试执行，但相同规范化签名的历史短时样本主导预测；精确签名已存在。'
                category, root = 'B', 'heterogeneous_exact_signature'
                repair = '区分解释器/依赖状态与测试工作量，建模同签名多模态分布；仅调整父边不能克服主节点样本。'
                secondary = ['D: 任务版本、测试数量、运行环境未充分观测，不能把全部残差归因缓存或负载']
            r['attribution'] = {'primary': category, 'root_cause_group': root, 'direct': direct,
                'secondary': secondary, 'confirmed_implementation_bug': False, 'repair_hypothesis': repair,
                'causal_limitations': '记录证明发生了何种执行/回退，未提供随机干预来量化各隐藏因素的因果占比。'}
            unique[r['id']] = r['attribution']
            appendix += [f"### {r['rank']}. {r['id']}", '',
                f"trace `{r['trace']['file']}:{r['trace']['line']}`；action `{r['trace']['action_id']}`。", '',
                '```sh', r['command'], '```', '',
                f"实际 {r['actual_ms']/1000:.6f}s；预测 {r['p50']/1000:.6f}s；绝对误差 {r['absolute_error_s']:.6f}s；log 误差 {r['log_error']:.6f}；桶 {r['actual_bucket']}→{r['predicted_bucket']}（差 {r['bucket_distance']}）。", '',
                f"**主因 {category} / {root}：** {direct} {repair}", '',
                '次因/边界：'+'；'.join(secondary)+'。', '']
            for d in r['clauses']:
                la, ed = d['lattice'], d['edge']; st = la['selected_summary']
                appendix += [f"子句 {d['clause_index']}：parse={d['parsed']['status']}，diagnostics={json.dumps(d['parsed']['diagnostics'], ensure_ascii=False)}。", '',
                    '```json', json.dumps(d['features'], ensure_ascii=False), '```', '',
                    f"Lattice exact={la['exact']}，候选 {len(la['candidates'])} 个；选中样本 n={st['n']}，min/median/max(ms)={st['min_ms']}/{st['median_ms']}/{st['max_ms']}。选中特征：", '',
                    '```json', json.dumps(la['selected_features'], ensure_ascii=False), '```', '',
                    f"Edge node={ed.get('node_id')}，exact={ed.get('exact_match')}，覆盖证据 n={ed.get('observation_count')}，父边 {len(ed.get('evidence', []))} 条。", '',
                    '| parent | 差集样本数 | κ | 混合占比 | 丢弃特征 |', '|---|---:|---:|---:|---|']
                for p in ed.get('evidence', []):
                    appendix.append(f"| {p['parent_id']} | {p['difference_count']} | {p['kappa']:.8g} | {p['mixture_share']:.8g} | `{json.dumps(p['dropped_features'], ensure_ascii=False)}` |")
                appendix += ['', '历史分布、全部候选 risk 及每个父边的桶分布保存在该案例 JSON 中。', '']
            appendix += ['原始执行输出末尾：', '', '```text', r['output_tail'], '```', '']
        write(path, rows); counts[model] = {'primary': dict(Counter(r['attribution']['primary'] for r in rows)),
            'root_cause_groups': dict(Counter(r['attribution']['root_cause_group'] for r in rows))}
    write(out/'attribution-counts.json', {'top30': counts, 'unique_calls': len(unique),
        'unique_primary': dict(Counter(v['primary'] for v in unique.values())),
        'confirmed_kb_prediction_bugs': 0,
        'scope': 'manual post-lock review of 60 ranked entries, not a population prevalence estimate'})
    (HERE/'worst-cases.md').write_text('\n'.join(appendix), encoding='utf8')

def interactions(out):
    # No test rows. Four observed cells are needed; effects are associations only.
    rows = [r for r in read(out/'events.json.gz') if r['partition'] in ('train', 'dev')]
    groups = defaultdict(list)
    for r in rows:
        e = r['event']; target = next((f for f in e['query']['features'] if f.startswith('target:')), '')
        groups[(r['partition'], e['query']['tool'], target)].append((
            {feature_key(f) for f in e['query']['features'] if f.startswith(('flag:', 'opt:', 'arg:', 'repeat:'))},
            math.log1p(e['outcome']['duration_ms']/1000)))
    results = []
    for (part, tool, target), rs in groups.items():
        keys = [k for k, n in Counter(k for ks, y in rs for k in ks).most_common(16) if n >= 10]
        for a, b in combinations(keys, 2):
            cells = defaultdict(list)
            for ks, y in rs: cells[f'{int(a in ks)}{int(b in ks)}'].append(y)
            if len(cells) != 4 or min(map(len, cells.values())) < 5: continue
            means = {k: statistics.mean(v) for k, v in cells.items()}
            results.append({'partition': part, 'tool': tool, 'target': target, 'keys': [a, b],
                'cells': {k: {'n': len(v), 'mean_log_seconds': means[k]} for k, v in cells.items()},
                'interaction_log': means['11']-means['10']-means['01']+means['00']})
    write(out/'parameter-interactions.json', {'rows': results,
        'limits': 'source-only 2x2 observational contrasts; task/workload and parameter-value confounding, no causal or heldout selection claim'})
    families = defaultdict(list)
    for r in rows:
        e = r['event']; target = next((f for f in e['query']['features'] if f.startswith('target:')), '')
        families[(r['partition'], e['query']['tool'], target)].append(e)
    associations = []
    for (part, tool, target), es in families.items():
        for key in sorted({feature_key(f) for e in es for f in e['query']['features'] if f.startswith(('opt:', 'flag:', 'arg:', 'repeat:'))}):
            values = defaultdict(list); present = []; absent = []
            for e in es:
                fs = [f for f in e['query']['features'] if feature_key(f) == key]
                y = e['outcome']['duration_ms']; (present if fs else absent).append(y)
                for f in fs: values[f].append(y)
            if len(present) < 3: continue
            associations.append({'partition': part, 'tool': tool, 'target': target, 'key': key,
                'present_n': len(present), 'absent_n': len(absent),
                'present_median_ms': statistics.median(present),
                'absent_median_ms': statistics.median(absent) if absent else None,
                'values': [{'feature': f, 'n': len(v), 'median_ms': statistics.median(v)} for f,v in sorted(values.items(), key=lambda x:-len(x[1]))]})
    write(out/'parameter-family-diagnostics.json', {'rows': associations,
        'scope': 'same source repository, partition, executable family AND module/subcommand target; presence/value associations, not causal importance'})

def syntax(out):
    summary = {}; rows = []
    for part in ('dev','test'):
        changes = read(out/'llm'/f'{part}-parse-changes.json.gz')
        counts = Counter(); tools = defaultdict(Counter)
        for r in changes:
            p = r['parsed_after']; argv = r['argv']; opts = [o for l in p['layers'] for o in l['options']]
            args = [a for l in p['layers'] for a in l['args']]; opaque = [a for l in p['layers'] for a in l['opaque']]
            counts[r['before']['status']+'->'+r['after']['status']] += 1
            tools[Path(argv[0]).name][p['status']] += 1
            expected_args = None; expected_opts = None
            if argv[0] == 'apt-cache' and len(argv)>1 and argv[1] in ('policy','search'):
                expected_args = argv[2:]; expected_opts = []
            elif argv == ['apt-get','update'] or argv == ['sort']:
                expected_args = []; expected_opts = []
            elif len(argv)==4 and argv[1:3]==['-m','ensurepip'] and argv[3] in ('--version','--upgrade'):
                expected_args = []; expected_opts = [(argv[3], [])]
            elif len(argv)==2 and argv[0]=='sort' and argv[1] in ('-h','-n','-u'):
                expected_args = []; expected_opts = [({'-h':'--human-numeric-sort','-n':'--numeric-sort','-u':'--unique'}[argv[1]], [])]
            if expected_args is not None:
                passed = args==expected_args and [(o['name'],o['values']) for o in opts]==expected_opts and not opaque and p['status']=='complete'
                rows.append({'id':r['id'],'partition':part,'argv':argv,'expected_args':expected_args,'expected_options':expected_opts,'passed':passed,'parsed':p})
        all_calls = [c for c in read(out/'calls.json.gz') if c['partition']==part]
        before = Counter(cl['command_features']['status'] for c in all_calls for cl in c['clauses'])
        after = before.copy()
        for r in changes:
            after[r['before']['status']] -= 1; after[r['after']['status']] += 1
        paths = {}
        for model in ('edge','lattice'):
            aa={r['id']:r for r in read(out/'llm'/f'{part}-{model}_original.json.gz')}
            bb={r['id']:r for r in read(out/'llm'/f'{part}-{model}_generated.json.gz')}
            paths[model] = {'context_changed':sum(aa[i]['context']!=bb[i]['context'] for i in aa)}
            if model=='edge':
                paths[model]['node_changed']=sum([d['node_id'] for d in aa[i]['decisions']]!=[d['node_id'] for d in bb[i]['decisions']] for i in aa)
                paths[model]['parent_set_changed']=sum([[p['parent_id'] for p in d['evidence']] for d in aa[i]['decisions']]!=[[p['parent_id'] for p in d['evidence']] for d in bb[i]['decisions']] for i in aa)
        summary[part] = {'changed_clauses':len(changes),'transitions':dict(counts),'by_entry':{k:dict(v) for k,v in tools.items()},
            'all_clause_status_before':dict(before),'all_clause_status_after':dict(after),'paths':paths}
    write(out/'llm/semantic-syntax-review.json', {'summary':summary,'simple_complete_forms_checked':len(rows),
        'passed':sum(r['passed'] for r in rows),'rows':rows,
        'partial_examples':{'apt-get install -y PACKAGE':'target install recognized; -y and operands remain opaque, not fully parsed',
            'sort -nr':'combined short options remain opaque; no false complete claim'},
        'limits':'independent expected names/values/positionals for unambiguous real forms; not a full grammar proof'})

def metadata(out):
    base = Path('baseline'); edge = read(base/'edge-kappa-kb.json'); lat = read(base/'clause-lattice-time-kb.json')
    v = read(out/'variant-manifest.json')
    effective={k:v for k,v in edge['config'].items() if k not in ('max_graph_nodes','max_covered_nodes')}
    v['configuration'] = {'bucket_edges_ms': [100, 500, 2000, 10000],
        'lattice_generation': lat.get('node_generation'), 'edge': effective,
        'edge_stored_legacy_config':edge['config'],
        'legacy_capacity_fields':'validated then removed by from_snapshot; not enforced capacity limits'}
    v['other_interfaces'].update(trie_legacy_median_composition='evaluated actual raw command interface, call target; interfaces/call-compatibility-summary.json',
        tool_legacy_conditional_p90='evaluated separately on whole-tool interval labels, not retained workload; interfaces/tool-legacy-p90-summary.json')
    write(out/'variant-manifest.json', v)
    write(out/'baseline-metadata.json', {'lattice_keys': list(lat), 'lattice_generation': lat.get('node_generation'),
        'edge_config': edge['config'], 'files': {p.name: {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in base.glob('*.json')}})
    split=read(out/'optimization-split.json')['tasks']
    path=out/'predictions.jsonl.gz';temporary=path.with_name('predictions-final.tmp.gz')
    with gzip.open(path,'rt',encoding='utf8') as src, gzip.open(temporary,'wt',encoding='utf8') as dest:
        for line in src:
            row=json.loads(line); part=split[row['task']]
            if row['partition']!=part:row.setdefault('initial_partition',row['partition'])
            row['partition']=part; pred=row['p50']; actual=row['actual_ms']
            row.update(absolute_error_s=abs(pred-actual)/1000 if pred is not None else None,
                log_error=abs(math.log1p(pred/1000)-math.log1p(actual/1000)) if pred is not None else None,
                bucket_distance=abs(row['predicted_bucket']-row['actual_bucket']) if pred is not None else None,
                evidence_applicable=pred is not None)
            dest.write(json.dumps(row,ensure_ascii=False)+'\n')
    temporary.replace(path)
    from tool_resource.runtime_kb import ClauseObservation
    from command_parser import ParseContext
    from command_parser.service import observation_features
    from research.kb_systematic_20261013.llm_rules import service_for
    services={}; changed=0; n=0
    for r in read(out/'observations.json.gz'):
        if r['partition']!='train':continue
        o=ClauseObservation(**r['observation']);n+=1
        if o.repo not in services:services[o.repo]=service_for(out,o.repo)
        f=services[o.repo].argv(o.argv,ParseContext(repo=o.repo,runtime_id='study-rules'))
        changed+=int(observation_features(o).features!=f.features)
    policy=read(out/'llm/rule-policy.json');policy.update(features_changed_training=changed,features_materialized_training=n)
    write(out/'llm/rule-policy.json',policy)

if __name__ == '__main__':
    out = HERE/'results'; annotate(out); interactions(out); syntax(out); metadata(out)
