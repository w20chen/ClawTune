import gzip
import hashlib
import html
import json
from pathlib import Path
import shutil

out=Path('research/kb_methods_fixed6_20261012')
summary=json.loads((out/'summary.json').read_bytes())
paired=json.loads((out/'paired_changes.json').read_bytes())
audit=json.loads((out/'abstention_audit.json').read_bytes())
report=json.loads(gzip.decompress((out/'report.json.gz').read_bytes()))
def table(headers,rows):
    return '<table><tr>'+''.join('<th>'+html.escape(str(v))+'</th>' for v in headers)+'</tr>'+''.join(
        '<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in r)+'</tr>' for r in rows)+'</table>'
before=table(['变体','前后共同可预测','原 MAE 秒','固定 6 MAE 秒','误差下降','误差上升','新增弃答'],[
    [m,r['count'],f"{r['old_mae_ms']/1000:.3f}",f"{r['new_mae_ms']/1000:.3f}",r['improved'],r['worsened'],r['lost']]
    for m,r in paired.items()])
resource_rows=[]
for m,v in report['models'].items():
    for r in v['metrics']:
        if r['target']!='duration_ms':
            resource_rows.append([m,r['target'],r['eligible'],r['predicted'],
                '—' if r['mae'] is None else f"{r['mae']:.4f}",r['unit']])
resources=table(['方法','资源','有效标签','可预测','MAE','单位'],resource_rows)
extra='<h2>固定 6 前后配对比较</h2><p>下表仅比较前后都能预测的同一批调用，以排除弃答变化造成的表面收益。</p>'+before
extra+=f"<p>新增弃答：{audit['id']}。实际耗时 {audit['actual_ms']/1000:.3f} 秒，存在 {audit['legal_candidate_nodes']} 个合法候选，无精确节点，触发仍保留的 {audit['candidate_limit']} 候选评分上限。详见 abstention_audit.json。</p>"
extra+='<h2>其他有效资源标签</h2><p>本次仅 CPU 峰值还有 122 条有效标签，其余资源没有通过当前评测条件的标签；不以零补齐。Oracle 仅用于耗时。</p>'+resources
page=(out/'report.html').read_text(encoding='utf8').replace('</html>',extra+'</html>')
(out/'report.html').write_text(page,encoding='utf8')
validation=out/'validation'
validation.mkdir(exist_ok=True)
for name in ('run_fixed6_offline.py','summarize_fixed6_offline.py','inspect_fixed6_results.py','audit_fixed6_abstention.py','finalize_fixed6_report.py','oracle-native-validation.json'):
    shutil.copyfile(Path('.runtime')/name,validation/name)
shutil.copyfile('.runtime/fixed6-summary-verification.log',validation/'summary-verification.log')
provenance=json.loads((out/'provenance.json').read_bytes())
provenance['verification'] += ['Tool, Trie and Edge duration rows identical to preceding runs (3185 each)',
    'added shrinkage abstention reproduced with 868 candidate nodes and existing limit 512',
    '1000 synthetic and 285 live native scorer checks exactly equal to production p50 without tolerance']
provenance['artifact_sha256']={p.relative_to(out).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
    for p in out.rglob('*') if p.is_file() and 'attempt1' not in p.parts and p.name!='provenance.json'}
(out/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf8')
assert '<h1>固定 6：七种 KB 方法离线评估</h1>' in page
assert all(phrase in page for phrase in ('固定 6 前后配对比较','其他有效资源标签','精确率','召回'))
print('Archived',len(provenance['artifact_sha256']),'artifacts; report content verified.')
print('Paired shrinkage improvement %',100*(1-paired['lattice']['new_mae_ms']/paired['lattice']['old_mae_ms']))
q=summary['oracle_comparison']['lattice']
print('Oracle vs shrinkage paired improvement %',100*(1-q['oracle_mae_ms']/q['model_mae_ms']))
