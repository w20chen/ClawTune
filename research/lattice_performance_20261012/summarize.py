"""Check exact equivalence, then write an auditable before/after report."""
import hashlib
import html
import json
from pathlib import Path

root=Path(__file__).parent
before=json.loads((root/'before/lattice6.json').read_text())
after=json.loads((root/'after-final/lattice6.json').read_text())
assert before['state_fingerprints']==after['state_fingerprints']
assert before['legacy_globals']==after['legacy_globals']
def records(folder):
    return [json.loads(line) for line in (root/folder/'lattice6-rows.jsonl').read_text().splitlines()]
old,new=records('before'),records('after-final')
assert len(old)==len(new)==1152
for a,b in zip(old,new):
    for key in a.keys()-{'match_ms','compose_ms'}:
        assert a[key]==b[key],(a['algorithm'],a['sample'],a['repeat'],key)
assert (root/'synthetic-before.json').read_bytes()==(root/'synthetic-after.json').read_bytes()
summary=dict(before=before,after=after,equivalence=dict(
    prediction_rows=len(new),resource_views=len(after['state_fingerprints'])-1,
    all_node_statistics_order_and_samples_exact=True,all_prediction_targets_and_evidence_exact=True,
    synthetic_resources_and_partial_causal_updates_exact=True),
    scope='128 fixed sampled calls, 3 selectors, 3 passes; no oracle; same fixed6 snapshots; prepared clauses; excludes HTTP, outer parsing, diagnostics and disk persistence',
    caveat='Restore/prepare is a full build. Update timing is one 32-observation successor batch, not an incremental per-observation update. Peak RSS is process high-water mark, not retained KB memory.')
(root/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
rows=[]
for name in before['query']:
    for part in ['match','combined']:
        a,b=before['query'][name][part],after['query'][name][part]
        rows.append(f'<tr><td>{name}</td><td>{part}</td><td>{a["p50_ms"]:.3f}</td><td>{b["p50_ms"]:.3f}</td><td>{a["p95_ms"]:.3f}</td><td>{b["p95_ms"]:.3f}</td></tr>')
page=f'''<!doctype html><meta charset="utf-8"><title>Lattice 等价加速验证</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;line-height:1.6}}table{{border-collapse:collapse}}td,th{{border:1px solid #bbb;padding:8px 16px;text-align:right}}th{{background:#edf2f7}}</style>
<h1>Lattice 固定 6：等价加速验证</h1>
<p>同一训练快照、固定 6 个可选特征上限。128 条固定抽样调用，3 种选择算法，各重复 3 次；不运行全量 oracle。</p>
<table><tr><th>算法</th><th>范围</th><th>旧 P50 ms</th><th>新 P50 ms</th><th>旧 P95 ms</th><th>新 P95 ms</th></tr>{''.join(rows)}</table>
<p>match 是全部目标的证据查询；combined 额外包含调用组合。排除公共外层解析、HTTP、诊断组装和磁盘写入。</p>
<p>完整恢复与建库：{before['ready_seconds']:.2f} → {after['ready_seconds']:.2f} 秒。</p>
<p>旧版验证全过程峰值 RSS：{before['peak_rss_mib']:.1f} MiB（未重建更新批次）。新版建库阶段峰值：{after['ready_peak_rss_mib']:.1f} MiB；新版含两代状态共存更新的全过程峰值：{after['peak_rss_mib']:.1f} MiB。阶段不同，不将两代更新峰值与旧版单代峰值直接比较；这些数也不等于常驻 KB 净内存。</p>
<p>新实现一次 32 条后台更新的 prepare：{after.get('update_prepare_ms',0)/1000:.2f} 秒。这仍然重建新一代统计，不能解释成逐条增量更新。</p>
<p>只查询时间的匹配 P50/P95：旧 {before['duration_only_match']['p50_ms']:.3f}/{before['duration_only_match']['p95_ms']:.3f} ms；新 {after['duration_only_match']['p50_ms']:.3f}/{after['duration_only_match']['p95_ms']:.3f} ms。</p>
<p>验证：全部节点字段、样本顺序和节点顺序的 SHA-256 完全一致；1,152 次查询的全部证据、资源输出、分桶概率及不可预测结果完全一致；额外资源与因果更新测试逐字节一致。</p>
<p>保持不变：候选生成上限 6、精确节点、执行入口约束、自身/覆盖样本、512 候选上限、统计与评分公式、候选顺序及可见时机。优化仅使用索引、重复计算复用与等价中位数下标运算。</p>
<p>旧 Edge 实验实际将上限降到 1，并且只预测时间；其历史耗时不可当作与固定 6 Lattice 的同规模对照。Edge 固定 6 须另做实验。</p>
<p><a href="summary.json">完整机器可读结果</a></p>'''
(root/'report.html').write_text(page,encoding='utf-8')
files=[p for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name!='provenance.json']
source=list(Path('services/sidecar/src/tool_time').rglob('*.py'))+[Path('services/sidecar/tests/test_lattice_performance_equivalence.py')]
provenance=dict(artifacts={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                current_sources={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in source})
(root/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
print(json.dumps(dict(equivalence=summary['equivalence'],before_seconds=before['ready_seconds'],after_seconds=after['ready_seconds'],query=after['query'],update_prepare_ms=after.get('update_prepare_ms')),indent=2))
