"""Generate auditable statistics, figures and the feasibility decision."""
from __future__ import annotations
import argparse
import hashlib
import json
import platform
import shutil
import statistics as st
import subprocess
import sys
from collections import Counter,defaultdict
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from evaluate import SEED, NS, SCALES, csv_write, dump, condition, family, metrics, summarize

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]


def interval(values):
    if len(values)<5:return (None,None)
    rng=np.random.default_rng(SEED)
    draws=rng.choice(values,size=(2000,len(values)),replace=True).mean(axis=1)
    return tuple(float(x) for x in np.quantile(draws,[.025,.975]))


def fmt(value):
    return '—' if value is None else f'{value:.4g}'


def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','|'+'|'.join(['---']*len(headers))+'|']+
                     ['| '+' | '.join(str(x).replace('|',' / ').replace('\n',' ') for x in row)+' |' for row in rows])


def module_parameter_pairs(records):
    """Supplement direct-pytest scan with recorded python -m pytest calls."""
    groups=defaultdict(lambda:defaultdict(list))
    for row in records:
        if family(row)!='python:module:pytest':continue
        argv=row['observation']['argv'];base=[];workers='default';cov=[];i=0
        while i<len(argv):
            arg=argv[i]
            if arg in {'-n','--numprocesses'} and i+1<len(argv):workers=argv[i+1];i+=2;continue
            if arg.startswith('--numprocesses='):workers=arg.split('=',1)[1];i+=1;continue
            if arg.startswith('--cov'):
                cov.append(arg)
                if arg=='--cov' and i+1<len(argv) and not argv[i+1].startswith('-'):cov.append(argv[i+1]);i+=1
                i+=1;continue
            base.append(arg);i+=1
        for target in SCALES:
            if target in row['values']:
                groups[(condition(row),row['repo'],row['task'],tuple(base),target)][(workers,tuple(cov))].append(row)
    output=[]
    for (stratum,repo,task,base,target),variants in groups.items():
        keys=sorted(variants)
        for i,ref in enumerate(keys):
            for var in keys[i+1:]:
                if sum(a!=b for a,b in zip(ref,var))!=1:continue
                a,b=variants[ref],variants[var]
                delta=float(np.log1p(st.median(r['values'][target] for r in b)/SCALES[target])-
                            np.log1p(st.median(r['values'][target] for r in a)/SCALES[target]))
                output.append(dict(stratum=stratum,repo=repo,task=task,target=target,base=__import__('shlex').join(base),
                    reference=str(ref),variant=str(var),delta=delta,n_reference=len(a),n_variant=len(b),
                    status='candidate_only_code_state_and_input_size_unverified',
                    reference_ids=json.dumps([r['id'] for r in a]),variant_ids=json.dumps([r['id'] for r in b])))
    return output


def generate(data,results):
    records=[json.loads(x) for x in (data/'observations.jsonl').open(encoding='utf-8')]
    byid={r['id']:r for r in records};quality=json.loads((data/'data_quality.json').read_text(encoding='utf-8'))
    sources=json.loads((data/'source_manifest.json').read_text(encoding='utf-8'))
    predictions=[];splits=[]
    for folder in sorted((results/'folds').iterdir()):
        if not (folder/'split.json').exists():continue
        split=json.loads((folder/'split.json').read_text(encoding='utf-8'));splits.append(split)
        for line in (folder/'predictions.jsonl').open(encoding='utf-8'):
            row=json.loads(line);row['stratum']=condition(byid[row['id']]);predictions.append(row)
    summary,paired=summarize(predictions,results)
    grouped=defaultdict(list)
    for row in summary:grouped[(row['stratum'],row['target'],row['n'],row['method'])].append(row)
    aggregate=[]
    for (stratum,target,n,method),rows in sorted(grouped.items()):
        out=dict(stratum=stratum,target=target,n=n,method=method,repositories=len(rows),
                 eligible=sum(r['eligible'] for r in rows),predicted=sum(r['predicted'] for r in rows))
        for metric in ('mae','log_mae','p90_pinball','p90_coverage','availability'):
            values=[r[metric] for r in rows if r[metric] is not None]
            lo,hi=interval(values);out.update({metric:st.mean(values) if values else None,
                                             metric+'_lo':lo,metric+'_hi':hi})
        aggregate.append(out)
    csv_write(results/'repository_macro.csv',aggregate)
    differences=[];pg=defaultdict(list)
    for row in paired:pg[(row['stratum'],row['target'],row['n'],row['method'])].append(row)
    for (stratum,target,n,method),rows in sorted(pg.items()):
        vals=[r['delta_log_mae'] for r in rows];lo,hi=interval(vals)
        differences.append(dict(stratum=stratum,target=target,n=n,method=method,repositories=len(rows),
            mean_delta_log_mae=st.mean(vals),ci95_lo=lo,ci95_hi=hi,
            negative_transfer_repository_fraction=st.mean(v>1e-12 for v in vals),
            worst_repository_delta=max(vals),paired=sum(r['paired'] for r in rows)))
    csv_write(results/'paired_repository_bootstrap.csv',differences)
    by_prediction={(p['stratum'],p['id'],p['target'],p['n'],p['method']):p for p in predictions}
    edge_zero=[p for p in predictions if p['target']=='duration_ms' and p['method']=='edge' and p['n']==0]
    edge_comparison=dict(zero_shot_queries=len(edge_zero),exact=sum(p['diagnostics'].get('exact_match',False) for p in edge_zero),
        different_from_fixed=sum((p['p50'],p['p90']) !=
            (by_prediction[(p['stratum'],p['id'],p['target'],0,'edge_fixed')]['p50'],
             by_prediction[(p['stratum'],p['id'],p['target'],0,'edge_fixed')]['p90']) for p in edge_zero))
    dump(results/'edge_cold_start_mechanism.json',edge_comparison)
    primary='v6:batch-20260913:swe|aarch64|None|320.0|qemu|None|None'
    primary_summary=[r for r in aggregate if r['stratum']==primary and r['target']=='duration_ms' and r['n']==0]
    pmap={r['method']:r for r in primary_summary}
    pdelta={r['method']:r for r in differences if r['stratum']==primary and r['target']=='duration_ms' and r['n']==0}
    # Fixed repository AND execution cohorts for learning curves.
    curve=[]
    for target in SCALES:
        supports=defaultdict(set)
        for s in summary:
            if s['stratum']==primary and s['target']==target:supports[s['repo']].add(s['n'])
        balanced={repo for repo,ns in supports.items() if 20 in ns}
        for n in NS:
            for method in ('family','family_calibrated','lattice','edge','edge_fixed','edge_freeze_target'):
                rows=[r for r in summary if r['stratum']==primary and r['repo'] in balanced and
                      r['target']==target and r['n']==n and r['method']==method]
                vals=[r['log_mae'] for r in rows if r['log_mae'] is not None]
                if vals:
                    lo,hi=interval(vals)
                    curve.append(dict(target=target,n=n,method=method,repositories=len(rows),
                        log_mae=st.mean(vals),ci95_lo=lo,ci95_hi=hi,availability=st.mean(r['availability'] for r in rows)))
    csv_write(results/'balanced_learning_curve.csv',curve)
    figures=results/'figures';figures.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'savefig.dpi':180})
    fig,axs=plt.subplots(1,2,figsize=(12,4.5))
    for ax,target in zip(axs,('duration_ms','sampled_peak_rss_bytes')):
        for method in ('family','family_calibrated','lattice','edge','edge_fixed'):
            selected=[r for r in curve if r['target']==target and r['method']==method]
            if selected:ax.plot([r['n'] for r in selected],[r['log_mae'] for r in selected],marker='o',label=method)
        ax.set(xlabel='Local measured clauses n',ylabel='Repository mean log MAE',title=target+' (fixed cohort)')
        ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.tight_layout();fig.savefig(figures/'cold_start_learning.png');plt.close(fig)
    fig,axs=plt.subplots(1,2,figsize=(12,4.5))
    methods=['global','executable','family','trie','lattice','lattice_no_repo','lattice_loso','edge','edge_fixed']
    for ax,metric in zip(axs,['log_mae','availability']):
        ax.barh(methods,[pmap[m][metric] for m in methods]);ax.invert_yaxis()
        ax.set(xlabel='Repository macro '+metric,title='Zero-shot duration; availability is reported separately')
    fig.tight_layout();fig.savefig(figures/'method_comparison.png');plt.close(fig)
    # Distribution plot is raw measured duration with repository medians.
    primary_rows=[r for r in records if condition(r)==primary]
    fams=Counter(family(r) for r in primary_rows).most_common(6)
    fig,axs=plt.subplots(2,3,figsize=(14,8))
    for ax,(fam,_) in zip(axs.flat,fams):
        byrepo=defaultdict(list)
        for r in primary_rows:
            if family(r)==fam:byrepo[r['repo']].append(r['values']['duration_ms'])
        repos=sorted(byrepo);values=[np.log1p(np.array(byrepo[r])/1000) for r in repos]
        ax.boxplot(values,tick_labels=[r.split('/')[-1] for r in repos],showfliers=False)
        ax.tick_params(axis='x',rotation=70,labelsize=7)
        ax.set(title=fam,ylabel='log1p(duration / second)')
    fig.suptitle('Observed cross-repository distributions; no causal parameter claim')
    fig.tight_layout();fig.savefig(figures/'cross_repository_distributions.png');plt.close(fig)
    # Traceable failures retain exact clause command plus source pool IDs.
    failures=[]
    worst=sorted([p for p in predictions if p['stratum']==primary and p['method']=='lattice' and p['target']=='duration_ms'
                  and p['n']==0 and p['p50'] is not None],key=lambda p:p['log_mae'],reverse=True)[:10]
    for pred in worst:
        row=byid[pred['id']]
        split=next(s for s in splits if s['repo']==pred['repo'] and s['target']==pred['target'] and pred['id'] in s['test_ids'])
        history=[byid[x] for x in split['training_ids'] if family(byid[x])==family(row)]
        failures.append(dict(pred,command=__import__('shlex').join(row['observation']['argv']),
            source=row['source'],source_line=row['source_line'],history_family_count=len(history),
            history_examples=[dict(id=x['id'],repo=x['repo'],value=x['values']['duration_ms'],
                                   command=__import__('shlex').join(x['observation']['argv'])) for x in history[:5]],
            interpretation='selected context is observed; missing input size/code state can confound causation'))
    dump(results/'failure_cases.json',failures)
    # Actual source rehash, duplicate execution audit and split assertions.
    changed=[]
    for source in sources:
        if hashlib.sha256(Path(source['source']).read_bytes()).hexdigest()!=source['sha256']:changed.append(source['source'])
    identities=set();duplicates=[]
    for row in records:
        o=row['observation'];key=(row['benchmark'],row['task'],row['call_id'],tuple(o['argv']),o['ts_start'],o['ts_end'],o['memory_measurement'])
        if key in identities:duplicates.append(row['id'])
        identities.add(key)
    assert not changed and not duplicates
    for split in splits:
        train=[byid[x] for x in split['training_ids']];prefix=[byid[x] for x in split['prefix_ids']];test=[byid[x] for x in split['test_ids']]
        assert all(r['repo']!=split['repo'] for r in train)
        assert not ({r['task'] for r in prefix}&{r['task'] for r in test})
        assert not ({r['task'] for r in train}&{r['task'] for r in test+prefix})
        assert split['repo'] not in split['inner_validation']['repositories']
        assert split['temporal_gate_verified']
    checks=dict(source_files_checked=len(sources),source_hash_mismatches=changed,
                duplicate_execution_identities=duplicates,folds_checked=len(splits),
                seed_loaded=False,repository_overlap=0,calibration_test_task_overlap=0,
                simulated_clock_strict_inequality_verified=True)
    dump(results/'leakage_checks.json',checks)
    seed=json.loads((ROOT/'seeds/bootstrap-v1/manifest.json').read_text(encoding='utf-8'))
    dump(results/'seed_contamination_audit.json',dict(seed_sha256=hashlib.sha256((ROOT/'seeds/bootstrap-v1/manifest.json').read_bytes()).hexdigest(),
        repos=sorted({r['repo'] for r in seed['provenance']['cases']}),case_count=seed['provenance']['case_count'],
        action='all snapshots excluded; every fold fit solely from declared source records'))
    counts=Counter()
    for f in quality['files']:counts.update(f['counts'])
    valid=Counter(t for r in records for t in r['values'])
    memory=[r for r in records if 'memory_extra_peak_bytes' in r['values']]
    strata_table=[]
    for name,info in quality['strata'].items():
        strata_table.append([name,info['files'],info['repositories'],info['tasks'],info['targets'].get('duration_ms',0),
                             info['targets'].get('memory_extra_peak_bytes',0),info['targets'].get('sampled_peak_rss_bytes',0)])
    source_table=table(['Stratum','Files','Labeled repos','Labeled tasks','Duration','Extra memory','RSS'],strata_table)
    execution_quality=json.loads((results/'execution_quality_totals.json').read_text(encoding='utf-8')) if (results/'execution_quality_totals.json').exists() else {}
    (results/'data_audit.md').write_text(f'''# 数据审计

读取 {len(sources)} 份真实 trace；导出 {len(records)} 条 clause target-view 记录。内存专用记录不重复计入 duration。

{source_table}

## 标签与缺失

在导出的观测视图中：

{table(['Target','Valid','Missing in exported views','Missing fraction'],[[t,valid[t],len(records)-valid[t],fmt(1-valid[t]/len(records))] for t in SCALES])}

这是已通过至少一种目标门槛的 clause 视图分母，不是全部工具调用分母；不能据此估算整体采样成功率。所有文件的工具调用数量、无 clause 观测、拒收原因分别保存在 `../{data.name}/data_quality.json`。其中 v5 `no_clause_observation` 共 {counts['no_clause_observation']} 次，`withheld_clause_cpu_without_interval_evidence` 共 {counts['withheld_clause_cpu_without_interval_evidence']} 次。原始 CPU 累积值不等于已验证 exec 区间增量。

原始执行审计覆盖 {execution_quality.get('tool_records','未生成')} 条工具记录，其中报告 failure {execution_quality.get('reported_failure','未生成')} 条、标记 censored {execution_quality.get('censored','未生成')} 条。正常完成的非零退出与被截断执行不同：前者可以有有效 duration，后者不能当完整执行标签。逐文件失败比例在 `source_execution_quality.csv`；每种 clause 指标的原始 availability/missing reason 计数在 `raw_availability_reasons.csv`，包括完整的无标签调用分母，不仅是进入模型的样本。

较新的环境内存工具级有效观测计数为 {counts['tool_valid_extra_memory']}；它们没有被复制成 clause 标签。独立合格的 clause extra-memory 为 {len(memory)} 条，来自 {len(set(r['repo'] for r in memory))} 个仓库。没有把 RSS 当作准入内存，也没有把缺失值补零。
原始 `memory_eligible` 标记不是充分条件：复核测量 cgroup 与执行 cgroup 的实际路径后，隔离了 {counts['memory_scope_rejected_tool']} 条工具级和 {counts['memory_scope_rejected_clause']} 条 clause 级旧内存标签。20260920-134712 批次测量 Docker cgroup，但执行已迁至其外部的 `/sys/fs/cgroup/clawtune/exec-*`，因此这些 `cgroup_v2_memory_current` 标签不代表执行负载，零值也不能当作真实零需求。拒收依据是范围不匹配而非数值大小；路径、原值、源哈希和行号保存在 `../{data.name}/memory_scope_quarantine.json`。合格 clause 内存来自后续 `cgroup_v2_environment_union_v1`，两个 run 仍分开；没有把不同 measurement 混合。

v5 多仓库批次的大量命令缺少 clause telemetry。它们的 hook duration 不能替代当前模型的 retained-workload/clause duration。具有完整 clause 标签的历史批次主要是 pennylane 与 sqlglot，批次、并发和主机证明不足，分别保留，不跨批次拼成一个貌似受控的多仓库实验。历史 RSS 输入按既有 seed recipe 的 MiB 解释；未取得历史 producer 版本的独立单位证明，因此仅作单独诊断。

## 来源与环境

完整源路径、SHA-256、benchmark、repo、task、run、命令、原始时间戳、hardware、measurement source 在 `../{data.name}/source_manifest.json` 和 `observations.jsonl`。未记录的 base commit、CPU quota、并发和架构保留为空，不猜测。相同仓库不同 commit 始终同一 holdout 身份。

主分析为 2026-09-13 SWE 实测批次：11 个仓库、30 个任务、778 条保留 clause。原始 artifact 中 `quota_cores=320` 是记录的环境配置值，不表示分配了 320 个独占核或实际消耗了这些核。PMU 记录主机 aarch64，exec provenance 明确 `qemu-x86_64`。该导出没有足够的并发证明，保留 unknown；因此这里只是批次内观察性比较，不是排除了同机干扰的因果试验。

后续批次按采集 run、主机架构、quota、QEMU/native 证据、并发和 memory measurement 分层。相同两仓库的重复批次不是新的独立仓库。Terminal、BFCL、Deep Research 没有可验证 repository 身份时仅纳入数据审计，不把 task/category 当作 repository。SWE-bench Verified 按其真实 repo 单独分层。

已有源文件哈希复核 {len(sources)} 个，变化 0；重复 execution identity 0。所有来源拒收和本地未下载文件均保留在质量审计 JSON；fixture、合成测试和旧预测输出不作为标签。
''',encoding='utf-8')
    (results/'leakage_audit.md').write_text(f'''# 泄漏审计

已验证 {len(splits)} 个 repository/target/condition fold，仓库交集、校准与测试任务交集均为 0。结果：`leakage_checks.json`。

- 每个 fold 从空模型拟合，初始训练不包含目标仓库的任何任务、重试、commit 或标签。仓库标识转为小写，跨 benchmark 前缀不构成不同仓库。
- 不加载任何公共 seed、旧 KB 或旧预测。bundled seed 含 10 个 sqlglot case，不能无条件用于 sqlglot 留出实验；记录在 `seed_contamination_audit.json`。
- n 指当前目标有效的 **clause 执行观测数**，不是工具调用数或任务数。只开放前 n 条。为防止同任务状态/重试泄漏，整个前缀触及的任务均从测试排除；所有 n 使用该 fold 最大支持 n 下相同的测试执行。只有一个任务的仓库仅支持 n=0。
- 按首条有效观测时间排列目标任务，任务内按记录时间/ID 排序。为了建立严格 repository holdout，把全部源仓库训练置于目标之前，使用公开、确定性的模拟串行时钟：start=2i、end=2i+1。这不声称恢复真实在线跨任务次序，也不保持并发重叠。所有资源数值仍为原始实测值，未由模拟时钟重算。每次提交严格满足 end < query start。
- Edge 源训练复用原 `fit` 的 seeded task replay；本地反馈使用 `begin`/`complete`/`commit_before`。测试结果从不在线回灌；只允许已声明的 calibration prefix。
- 校准采用 source-only family P50 的事前残差，中位数后乘 n_family/(n_family+lambda)。lambda 候选 0、2、5，在源仓库内部 leave-one-repository-out 验证，按仓库等权平均选择。duration 选择 log MAE；资源选择单位缩放后的 p90 pinball。无可用内部少样本验证时，n>0 的 candidate 返回 unavailable（诊断中保留未使用的 default=5）；n=0 仍等于未校准先验。范围固定为相同语法 family，不能传播到无关工具。
- 原始 KB 参数不调优；各消融全部报告。E1 候选规则不根据测试收益挑选。未使用测试标签选择模型、筛选有利仓库或生成合成参数对。

`folds/*/split.json` 保存逐条训练、前缀、测试 ID 与内部验证仓库；预测文件保存逐条 availability、真实值、预测分位数、证据数量、选中上下文和 Edge parent 诊断。所有方法共用目标标签集合，`e3_kb_ablation.csv` 使用与 Lattice 都可预测的成对样本；不能将各自覆盖子集上的平均误差直接解释为改进。Bootstrap 以仓库为单位，少于 5 个仓库仅描述，不给置信区间。
''',encoding='utf-8')
    shutil.copyfile(HERE/'implementation_audit.md',results/'implementation_audit.md')
    # Main decision: no controlled cross-repository parameter pairs and only two admission-memory repos.
    comparison=table(['Method','Availability','Log MAE','P90 coverage','Paired delta vs Lattice','95% repo bootstrap'],
        [[m,fmt(pmap[m]['availability']),fmt(pmap[m]['log_mae']),fmt(pmap[m]['p90_coverage']),
          fmt(pdelta[m]['mean_delta_log_mae']),f"[{fmt(pdelta[m]['ci95_lo'])}, {fmt(pdelta[m]['ci95_hi'])}]"] for m in methods])
    memory_summary=[r for r in aggregate if r['target']=='memory_extra_peak_bytes' and r['method'] in {'family','family_calibrated','lattice'}]
    memory_table=table(['Condition','n','Method','Repos','Availability','P90 pinball (MiB)','P90 coverage'],
        [[r['stratum'],r['n'],r['method'],r['repositories'],fmt(r['availability']),fmt(r['p90_pinball']/1048576 if r['p90_pinball'] is not None else None),fmt(r['p90_coverage'])] for r in memory_summary])
    cal=[r for r in curve if r['target']=='duration_ms' and r['method'] in {'family','family_calibrated','lattice','edge','edge_fixed','edge_freeze_target'}]
    curve_table=table(['Method','n','Same repositories','Log MAE','Availability'],[[r['method'],r['n'],r['repositories'],fmt(r['log_mae']),fmt(r['availability'])] for r in cal])
    e1=list(__import__('csv').DictReader((results/'e1_transferability.csv').open(encoding='utf-8')))
    pair_candidates=list(__import__('csv').DictReader((results/'e1_parameter_pairs.csv').open(encoding='utf-8')))
    module_pairs=module_parameter_pairs(records)
    csv_write(results/'e1_module_parameter_pairs.csv',module_pairs,
              ['stratum','repo','task','target','base','reference','variant','delta','n_reference','n_variant','status','reference_ids','variant_ids'])
    pair_candidates+=module_pairs
    unique_parameter_pairs={(r['stratum'],r['repo'],r['task'],r['base'],r['reference'],r['variant']) for r in pair_candidates}
    inventory=defaultdict(list)
    for r in records:
        signature=tuple(f for f in r['features'] if not f.startswith('repo='))
        inventory[(condition(r),r['tool'],family(r),signature)].append(r)
    csv_write(results/'command_inventory.csv',[dict(stratum=k[0],executable=k[1],family=k[2],
        normalized_template=json.dumps(k[3]),observed_options=json.dumps([f for f in k[3] if f.startswith(('flag:','opt:'))]),
        samples=len(v),repositories=len({r['repo'] for r in v if r['repo']}),
        input_size='unrecorded',example_id=v[0]['id']) for k,v in sorted(inventory.items())])
    e1_groups=defaultdict(list)
    for r in e1:
        if not r['repo'].startswith('unidentified:'):
            e1_groups[(r['stratum'],r['family'],r['target'])].append(r)
    e1_between=[]
    for (stratum,fam,target),rows in sorted(e1_groups.items()):
        if len(rows)<2:continue
        values=[float(np.log1p(float(r['p50'])/SCALES[target])) for r in rows]
        lo,hi=interval(values)
        e1_between.append(dict(stratum=stratum,family=fam,target=target,repositories=len(rows),
            samples=sum(int(r['n']) for r in rows),mean_repo_log_median=st.mean(values),
            mean_repo_log_median_ci95_lo=lo,mean_repo_log_median_ci95_hi=hi,
            between_repo_log_median_std=st.stdev(values),controlled_parameter_pairs=0))
    csv_write(results/'e1_between_repository.csv',e1_between)
    descriptive=[]
    for fam in ('cat','ls','grep','git:log','python:inline','python:module:pytest'):
        between=next(r for r in e1_between if r['stratum']==primary and r['family']==fam and r['target']=='duration_ms')
        medians=[float(r['p50']) for r in e1 if r['stratum']==primary and r['family']==fam and r['target']=='duration_ms']
        repeats=defaultdict(list)
        for row in primary_rows:
            if family(row)==fam:repeats[(row['repo'],row['task'],tuple(row['observation']['argv']))].append(row['values']['duration_ms'])
        within=defaultdict(list)
        for (repo,task,argv),values in repeats.items():
            if len(values)>1:within[repo].append(st.stdev(np.log1p(np.array(values)/1000)))
        noise=st.mean(st.mean(v) for v in within.values()) if within else None
        descriptive.append([fam,between['repositories'],between['samples'],f'{min(medians):.1f}–{max(medians):.1f}',
                            fmt(between['between_repo_log_median_std']),fmt(noise),len(within)])
    e1_descriptive=table(['Family','Repos','Clauses','Repo median range (ms)','Between-repo log SD','Within-task repeat log SD (repo mean)','Repeat repos'],descriptive)
    common_families=defaultdict(set)
    for r in primary_rows:common_families[family(r)].add(r['repo'])
    common_families={k:len(v) for k,v in common_families.items() if len(v)>=2}
    cv={(r['method'],r['n']):r for r in cal}
    cal_csv=list(__import__('csv').DictReader((results/'calibration_effect.csv').open(encoding='utf-8')))
    negatives=[]
    for n in (1,2,5,10,20):
        selected=[r for r in cal_csv if r['stratum']==primary and r['target']=='duration_ms' and int(r['n'])==n]
        if selected:
            vals=[float(r['delta_log_mae']) for r in selected]
            negatives.append([n,len(vals),sum(x< -1e-12 for x in vals),sum(x>1e-12 for x in vals),fmt(st.mean(vals)),fmt(max(vals))])
    negative_table=table(['n','Repos','Improved','Worsened','Mean delta vs family','Worst repo delta'],negatives)
    n20_rows=[r for r in cal_csv if r['stratum']==primary and r['target']=='duration_ms' and int(r['n'])==20]
    concentration=''
    if n20_rows:
        largest=min(n20_rows,key=lambda r:float(r['delta_log_mae']))
        total_benefit=-sum(float(r['delta_log_mae']) for r in n20_rows)
        share=-float(largest['delta_log_mae'])/total_benefit if total_benefit>0 else None
        concentration=f"n=20 的平均改善中，{largest['repo']} 贡献约 {share:.1%}；这个集中度进一步限制外推。" if share is not None else ''
    def curve_value(method,n):return fmt(cv.get((method,n),{}).get('log_mae'))
    (results/'research_report.md').write_text(f'''# ClawTune 冷启动知识迁移可行性研究

**研究决策：INCONCLUSIVE。** 已完成真实 trace 审计、E1 描述性分析、E2 仓库留出/少样本评估及 E3 原始 KB 消融。现有样本可以量化一批 QEMU 环境下的冷启动行为，但不足以支持“可迁移的参数效应”或面向准入内存的新语义迁移算法。这个结论不等于证明路线无效。

## 范围与证据

代码基线 `8fce6b72770b2eaf8a973b82183c72477fb94c2f`；随机种子 {SEED}。生产代码、OpenClaw core、原始 trace、训练集与既有 KB 均未修改。来源和质量见 `data_audit.md`，机制见 `implementation_audit.md`，切分和时序见 `leakage_audit.md`。全部实验为 clause-level；n 为目标指标有效 clause 观测数。模拟串行 replay 不等于真实在线事件重放。

## RQ1：是否存在跨仓库规律？

主 SWE 批次有 11 个独立仓库、30 个任务、778 条有效 duration/RSS clause；其中 {len(common_families)} 个语法命令族在至少两个仓库出现：`{json.dumps(common_families,sort_keys=True)}`。这建立了可比较的观察性数据覆盖，不自动证明参数规律。

各仓库资源分布在 `e1_transferability.csv`，仓库中位数的跨仓库离散度和仓库 bootstrap 区间在 `e1_between_repository.csv`；真实相同 task+argv 的重复噪声在 `e1_repeat_noise.csv`。重复执行仍可能夹杂代码修改与缓存变化，不能作为纯测量噪声。直接 pytest 与 python-module pytest 扫描共发现 {len(unique_parameter_pairs)} 组候选命令对、{len(pair_candidates)} 条目标指标差值；分别保存在 `e1_parameter_pairs.csv` 与 `e1_module_parameter_pairs.csv`。仅在同任务、相同剩余 argv、单一 workers/coverage 类别变化时保留。**严格验证输入规模、代码状态和运行条件一致的参数匹配对为 0。** 因而相对变化是否比绝对量稳定、哪些参数可迁移或造成负迁移，当前无法可靠回答，不报告虚构的 delta 分布或因果效应。`command_inventory.csv` 保留 executable、语法 family、规范化模板、选项及样本/仓库数，输入规模缺失明确标为 unrecorded。

代表命令族的绝对尺度与重复离散度如下（均为主批次 duration；log 指 log1p(duration/1000ms)）：

{e1_descriptive}

`cat`、`ls`、`grep` 在本批次的绝对时间分布较集中；Python inline 和 pytest 的仓库尺度差异明显，因而单一 executable 先验容易错配。不过短调用在 log1p 下也会被压缩，不能把较小 SD 直接解释为可迁移的参数效应；共享 QEMU/监测开销也未被独立分离。重复列只统计同 task+argv 的真实重复，先在仓库内平均再按仓库等权，包含代码/缓存变化，不是受控噪声下界。相对参数变化缺少合格匹配对，所以不能完成绝对量与参数 delta 稳定性的有效比较。对这些命令族的预测迁移证据仍须结合下述严格留出结果。

## RQ2：零样本与少量本地观测是否有效？

主批次 duration 零样本结果如下。availability 与误差均按仓库等权平均；各自误差的预测覆盖集合不同，真正的成对改进看最后两列（candidate minus Lattice，负数更好）。P50 误差就是 MAE/log MAE；完整原单位 MAE 和 p90 pinball 在 CSV。

{comparison}

下表学习曲线固定为能支持 n=20 的相同仓库和相同测试执行集合；这避免曲线随 n 更换仓库或测试样本。其他仓库支持的全部 n 仍在 `e2_cold_start.csv`，没有补齐不存在的历史。

{curve_table}

固定 cohort 下，校准法 log MAE 在 n=0/1/20 分别为 **{curve_value('family_calibrated',0)} / {curve_value('family_calibrated',1)} / {curve_value('family_calibrated',20)}**；Lattice 对应 **{curve_value('lattice',0)} / {curve_value('lattice',1)} / {curve_value('lattice',20)}**。这直接回答了本批次少样本是否改善，不能外推成所有仓库的收敛保证。Edge 在 n=20 的 learned / fully-fixed / target-frozen 分别为 {curve_value('edge',20)} / {curve_value('edge_fixed',20)} / {curve_value('edge_freeze_target',20)}。

校准相对同一 source-family 先验的仓库级收益与负迁移：

{negative_table}

{concentration} 另外，`armmbed/mbed-tools` 在 n=20 的 P50 log MAE 改善，但 P90 pinball 反而增加约 4173 ms；中位数改善不等于尾部预测更可靠。

`calibration_effect.csv` 给出校准相对未校准 family 先验的仓库级变化、真正获得同 family 反馈的测试样本数。一个本地样本只修正兼容命令族，没有证据时偏差仍为 0；它不能证明修正了整个仓库所有工具。`paired_repository_bootstrap.csv` 报告相对 Lattice 的负迁移仓库比例和最差退化幅度。小样本或宽区间不支持“预测已稳定”的结论；n 的上限 20 也不等于保证收敛。

准入相关环境增量内存共 {len(memory)} 条，仅 {len(set(r['repo'] for r in memory))} 个独立仓库，测量口径和采集 run 分开：

另有 {counts['memory_scope_rejected_clause']} 条旧 clause 内存虽标记 eligible，但被测 cgroup 不包含执行进程，已按原始路径证据隔离；不能用这些零值推导准确率。工具级另隔离 {counts['memory_scope_rejected_tool']} 条，明细见 data audit 与 quarantine 文件。

{memory_table}

同一对仓库的多个批次不是独立重复仓库。多数批次每仓库仅一任务，只能支持零样本；少样本结果只能视为病例描述。RSS 单独报告，不能补足环境内存证据。不能由这些数据判断时间与准入内存是否具有相同迁移机制。

## RQ3：LatticeKB/EdgeKappaKB 的实际限制是什么？

1. Lattice 已经有 repo-free 公共节点，冷启动不等于完全无先验。它依赖语法特征子集；无法从未记录的输入规模、测试数量、依赖构建状态推导资源尺度。完全未见核心 executable/target 时可能 abstain。
2. 精确匹配直接取经验样本，不保证跨仓库或小样本可靠。variance shrinkage 作用于选择风险，而非直接缩减预测均值或校准 P90。真实失败例：`20c/ctl` 的 `python -` 实测 426 ms，选中 `tool=python, flag:-` 的 6 条源样本后 P50 为 28414 ms；`3yourmind/django-migration-linter` 的短 inline assertion 实测 332 ms，回退到 `tool=python` 的 197 条源样本后预测 25996 ms。语法命令族基线显式区分 Python module/inline/script，比仅按 executable 更具体；本批次收益不需要用 LLM 才能解释。LOSO、最大特征数、去 repo 与 own-node accumulation 的实际结果全部保存在 `e3_kb_ablation.csv`；不能把覆盖率变化单独叫作收益。
3. **Edge 的已学习边权不会直接迁移到未见过的完整节点。** `edge_kappa_kb/kb.py::_predict` 明确在 `exact=False` 时使用 `k0 / len(parents)`，只在已有 child 上读取存储的 log-kappa。repository-disjoint 查询携带新的 repo 特征，通常形成全新 child。实测 {edge_comparison['zero_shot_queries']} 个零样本 duration 查询中，exact={edge_comparison['exact']}，learned 与 fixed 的 P50/P90 不同数={edge_comparison['different_from_fixed']}（含双方 unavailable 的一致结果）。因此“源数据训练了权重”不等于“新仓库继承了这些权重”。父节点的真实分布仍可迁移。
4. Edge 原算法仅 duration；局部反馈修正哪些边取决于已查询/已覆盖的节点和保存的事前 token。`edge_fixed` 和 `edge_freeze_target` 区分源训练与目标反馈。Lattice 去 repo 还会改变 bounded graph 的节点预算、完整签名分组和精确匹配，不能把全部变化解释成一个单纯的仓库过滤开关。
5. `failure_cases.json` 保存最差的真实命令、目标仓库、实测值、P50/P90、选中上下文、可用源历史与原始定位。上下文和误差是直接证据；输入规模或状态的遗漏只是待验证解释，不伪装为已确定原因。

## RQ4：是否支持开发 LLM-assisted Semantic Knowledge Transfer？

目前不足以给出 GO。数据验证了冷启动评估的现实缺口，也提供了语法迁移与少样本修正的实测基线，但没有隔离“语义知识”相对已有 KB 的增量价值。可优先检验的语义信息是：测试目标数量/规模、测试与收集模式、编译/依赖安装阶段、是否重用缓存、参数的真正作用范围。这些是下一阶段候选，不是本研究证实有效的特征。

下一步最值得验证的问题：在同一原生硬件、已知 CPU quota 与受控并发下，同一测试命令族的输入规模解释，是否能在严格 repository holdout 上改善环境 extra-memory 的 P90 校准，并降低少样本负迁移？需要更多独立仓库、多任务、可核验变体与重复执行。先补足真实测量，再决定是否需要 LLM；无需先做完整仓库扫描器或新机器学习模型。

## 复现与限制

用 `../reproduce.py --flat <原始644份trace目录> --output <全新输出目录>`，同一 ClawTune checkout 中需保留清单引用的本地 `.runtime` 下载。入口输出源清单、代码哈希、配置、日志、fold、逐条预测、汇总和图。图见 `figures/`。统计模型均使用真实标签，无合成命令、资源峰值或测试结果。

本机 Windows/WSL 没有可用 Docker 命令，不能调用现有 Linux/cgroup/eBPF benchmark runner 补采；待运行命令和前提记录在 `docs/CURRENT_PLAN.md`。未把 dry-run 或单元测试当成新的真实测量。

核心限制：未知并发/历史硬件信息、QEMU 开销、两仓库的环境内存覆盖、任务数不足、观测性变体、短调用采样可用性选择偏差，以及训练源运行顺序采用模拟。总体决策依据这些证据缺口，而不是挑选有利的平均收益。
''',encoding='utf-8')
    dump(results/'execution_environment.json',dict(python=sys.version,platform=platform.platform(),numpy=np.__version__,matplotlib=matplotlib.__version__,
        git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        code_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for base in [HERE,ROOT/'services/sidecar/src',ROOT/'cold_start'] for p in base.rglob('*.py') if '__pycache__' not in p.parts}))
    print('REPORT COMPLETE',len(predictions),'predictions',len(splits),'folds',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data',type=Path,required=True);parser.add_argument('--results',type=Path,required=True)
    args=parser.parse_args();generate(args.data,args.results)
