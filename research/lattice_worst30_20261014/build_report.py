"""Render the audited cases as a standalone, inspectable HTML report."""
import gzip
import html
import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent

def read(name):
    p = OUT / name
    return json.loads(gzip.decompress(p.read_bytes()) if p.suffix == '.gz' else p.read_bytes())

def esc(value):
    return html.escape(str(value))

def seconds(value):
    return '不可用' if value is None else f'{value / 1000:.3f}'

def main():
    rows = read('worst30-evidence.json.gz')
    summary = read('summary.json')
    review = read('review.json')
    corrected = {r['id']: r for r in read('context-predictions.json.gz')}
    context_summary = read('context-summary.json')
    training_audit = {(r['rank'], r['clause']): r for r in read('training-output-audit.json.gz')}
    body = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<title>LatticeKB 最坏 30 次调用：2026-10-14</title>',
        '<style>body{font:15px/1.6 system-ui,sans-serif;max-width:1500px;margin:32px auto;padding:0 24px;color:#17202a}table{border-collapse:collapse;width:100%;margin:20px 0}th,td{border:1px solid #d5d8dc;padding:7px;vertical-align:top}th{background:#eaf2f8;position:sticky;top:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f7;padding:12px}a{color:#145a9b}details{margin:16px 0;border:1px solid #ccd1d1;padding:12px}summary{font-weight:700;cursor:pointer}.num{white-space:nowrap;text-align:right}</style>',
        '<h1>当前 LatticeKB：绝对耗时误差最大的 30 次调用</h1>',
        '<p>本次重新读取原始 SWE-rebench trace，训练当前生产代码的 LatticeKB，冻结后预测。算法为 shrinkage，可选特征组合上限为 6，seed=42；仅对 exec、terminal_exec 评分。排序依据为 |调用耗时 p50 − 合格的实际耗时标签|。主排名未应用额外上下文修正；修正结果作为独立对照。时间单位均为秒。</p>',
        '<p>原因分析使用测试输出进行事后诊断，没有把这些输出或耗时用于训练或选参数。样本极端误差不是总体错误构成的无偏估计。调用耗时标签可能与工具原始跨度不同。</p>',
        '<p>这 30 次调用来自 18 个任务，26 次低估、4 次高估，占全部可评分调用绝对误差总和的约 46.98%。10 项主要是回退时丢失范围或配置，10 项是同一命令混合提前失败与正常工作，3 项是无同配置样本而使用带额外参数的样本，3 项只有两条不具代表性的同特征样本，3 项计入了未执行阶段，1 项混淆构建与测试目标。类别表示主要的已观察机制，可能存在次要原因。</p>',
        '<p>额外修正改变本表 7 项，均改善，其余 23 项不变。查看 <a href="corrected-worst30.json">应用额外修正后重新排名的 30 项</a>。整体可评分调用中，70 项改善、24 项恶化，不能只报告平均误差下降。</p>',
        '<pre>' + esc(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre>',
        '<h2>额外修正的独立对照</h2><pre>' + esc(json.dumps(context_summary, ensure_ascii=False, indent=2)) + '</pre>',
        '<h2>原因汇总</h2><pre>' + esc(json.dumps(Counter(review[r['id']]['category'] for r in rows), ensure_ascii=False, indent=2)) + '</pre>',
        '<table><thead><tr><th>#</th><th>任务及调用索引</th><th>命令摘要</th><th>实际</th><th>Lattice p50</th><th>绝对误差</th><th>额外修正后 p50</th><th>原因及证据</th></tr></thead><tbody>']
    for r in rows:
        a, c = review[r['id']], corrected[r['id']]
        body.append(f'<tr><td><a href="#case{r["rank"]}">{r["rank"]}</a></td><td>{esc(r["id"])}</td><td>{esc(a["command_summary"])}</td>' +
            ''.join(f'<td class="num">{seconds(v)}</td>' for v in (r['actual_ms'], r['p50_ms'], r['absolute_error_ms'], c['corrected_p50_ms'])) +
            f'<td>{esc(a["reason"])}</td></tr>')
    body.append('</tbody></table><h2>逐项证据</h2>')
    for r in rows:
        a, c = review[r['id']], corrected[r['id']]
        body.extend([f'<details id="case{r["rank"]}"><summary>#{r["rank"]} {esc(r["id"])} — {esc(a["category"])}</summary>',
            '<p>' + esc(a['reason']) + '</p><p>' + esc(a.get('limitation', '')) + '</p>',
            '<h3>完整命令</h3><pre>' + esc(r['command']) + '</pre>',
            '<h3>原始记录位置</h3><pre>' + esc(json.dumps(r['trace'], ensure_ascii=False, indent=2)) + '</pre>',
            '<h3>输出证据</h3><pre>' + esc(a['output_evidence']) + '</pre>',
            '<h3>额外修正</h3><pre>' + esc(json.dumps({'p50_ms': c['corrected_p50_ms'], 'support': c['context_support'],
                'reason': c['correction']['reason'] if c['correction'] else None}, ensure_ascii=False, indent=2)) + '</pre>'])
        for ci, cl in enumerate(r['clause_evidence']):
            body.append('<h3>子命令预测依据</h3><pre>' + esc(json.dumps({k: v for k, v in cl.items() if k not in ('values_ms', 'training_sources')}, ensure_ascii=False, indent=2)) + '</pre>')
            audit = training_audit[(r['rank'], ci)]
            body.append('<h3>历史调用输出核对</h3><pre>' + esc(json.dumps({k:v for k,v in audit.items() if k != 'samples'}, ensure_ascii=False, indent=2)) + '</pre>')
            ordered = sorted(audit['samples'], key=lambda s:s['duration_ms'])
            examples = ordered if len(ordered) <= 10 else ordered[:3] + ordered[-3:]
            body.append('<details><summary>训练输出示例及原始 trace 位置（短耗时与长耗时）</summary><pre>' + esc(json.dumps(examples, ensure_ascii=False, indent=2)) + '</pre></details>')
            body.append('<details><summary>全部训练来源及各次耗时（毫秒）</summary><pre>' + esc(json.dumps(cl['training_sources'], ensure_ascii=False, indent=2)) + '</pre></details>')
        body.append('</details>')
    body.append('</html>')
    (OUT / 'report.html').write_text('\n'.join(body), encoding='utf8')

if __name__ == '__main__':
    main()
