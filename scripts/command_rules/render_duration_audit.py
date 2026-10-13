"""Package the duration attribution evidence as JSON and a standalone HTML report."""
from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import statistics
from collections import Counter
from pathlib import Path

NOTES = [
    ("大范围测试回退", "pytest returntypes/tape/transforms", "语法完整，但测试目录组合没有匹配到可用的专门节点；Lattice/Edge 使用同仓库 pytest -q 的 94 条混合记录（53 种 argv），中位数 5.33 秒。实际运行数千测试，pytest 自报 595.47 秒。"),
    ("入口约束丢失", "pytest 多目录", "Lattice 选中 tool=python 的 3193 条全局 Python 历史，未保留 pytest 入口。Edge 保留部分测试目录信息，预测 84.20 秒，仍明显偏短。存在 --no-flaky-report 未知选项及 1 项特征截断，但目录信息并未全部丢失，不能把它们当作唯一原因。"),
    ("未知工具混淆", "cargo fmt --check", "缺少 cargo 规则，fmt/test 没有形成不同的核心入口；唯一 cargo 训练样本是 cargo test，耗时 538.52 秒，被借给 fmt。实际输出明确为 cargo-fmt 未安装，立即失败；后续 && 命令也未执行。旧版此调用不提供预测。"),
    ("入口约束丢失", "pytest tests/returntypes", "解析 complete，仍选中通用 Python 节点 1.33 秒；Edge 回退同仓库 pytest -q 的 649 条混合记录，预测 3.57 秒。实际 returntypes 测试执行 478.86 秒。旧版 Lattice 同样失败，证明不是单纯的新解析问题。"),
    ("构建历史不足、条件执行", "pip install && make install-dev-rs && make test-rs", "训练中没有 make install-dev-rs 的专门样本，退到以 make test 为主的 90 条记录。实际 install-dev-rs 的 Rust 构建运行 575.34 秒后失败；test-rs 没有执行，但合成器仍把其约 112.27 秒的历史计入。旧版整条调用不提供预测。"),
    ("全量测试分布过宽", "pytest tests --deselect ...", "Lattice 使用 tests 目录节点 116 条历史，中位数 62.59 秒，范围 0.27–518.57 秒；实际全量测试 428.42 秒。--no-flaky-report 后的参数未继续解析，但 tests 已保留。选择了相关族仍缺少本次运行规模与状态信息。"),
    ("同形命令样本稀少", "pytest -q tests（限制线程）", "Lattice 精确特征节点只有 2 条记录，33.50/51.89 秒，取中位数 42.70 秒；当前项目状态下跑了 16288 个通过测试，pytest 自报 409.73 秒。环境变量不在本次 KB 匹配特征中；不能仅凭相同 argv 认为工作量相同。"),
    ("未知选项吞并目录", "pytest -n auto tests/devices", "-n 未被规则识别，连同 auto、--tb=short、tests/devices 整体成为 opaque；丢弃这一特征时目录也一起丢失。旧版可命中 tests/devices、-n auto 的 383.23 秒样本；新版 Lattice 回退 Python 1.33 秒，Edge 回退混合 pytest 3.57 秒，再合成 tail。"),
    ("同形命令样本稀少", "pytest -q tests", "与第 7 条命中同一个只有 2 条记录的节点，Lattice 预测 42.70 秒；实际 pytest 自报 384.48 秒。相同仓库名及命令字符串没有区分 task 对应版本和执行状态。"),
    ("入口约束丢失", "pytest tests/gradients（第 3 次）", "语法完整，Lattice 仍选通用 Python 节点 1.33 秒。Edge 使用更具体父节点混合预测 87.64 秒，仍低于实际约 349 秒。三次 gradients 执行都是独立调用，但属于同一任务，不能当作三个独立任务的证据。"),
    ("入口约束丢失", "pytest tests/gradients（第 1 次）", "与第 10 条相同的确定性预测，实际约 348 秒；不是命令参数没有被识别，而是节点选择和统计分布无法描述这次完整 gradients 工作量。"),
    ("入口约束丢失", "pytest tests/gradients（第 2 次）", "与第 10 条相同的节点选择，实际约 346 秒。旧版 Lattice 也为 1.33 秒；本问题跨版本存在。"),
    ("入口约束丢失", "pytest numpy/ops/.../tape", "解析完整且保留目录，Lattice 却选通用 Python 节点，Edge 使用同仓库 pytest -q 混合父节点。实际 10037 个测试通过，pytest 自报 267.82 秒。"),
    ("未知选项吞并目录", "pytest -n 4 data/fermi/kernels/...", "-n 之后的工作进程数和所有测试目录被合并为 opaque；两后端回退到广泛历史。实际有 391 个失败、1277 个通过测试，pytest 自报 262.38 秒，失败并不等于快速退出。"),
    ("多目录覆盖不足", "pytest templates transforms", "Lattice 只保留第一个目录 tests/templates，对应 4 条训练记录，7.996–43.536 秒，中位数 25.37 秒；没有覆盖追加 transforms 带来的实际工作量。Edge 同样预测 25.37 秒。"),
    ("入口约束丢失、状态未表达", "pytest --lf -x -vv", "--lf 等选项已被识别，Lattice 仍退到 Python 根节点；last-failed 依赖仓库中的 pytest 缓存状态，这里没有进入特征。实际输出 631 passed、96 deselected、1 error，耗时 216.93 秒。"),
    ("未知选项吞并目录", "pytest -n 8 tests", "旧版保留 -n=8 和 tests，新版把 -n 8 tests 合为 opaque。Lattice 从包含 89 条记录的 117.62 秒节点退到 1.33 秒；实际测试 215.00 秒。"),
    ("第二阶段失去测试规模", "定向 pytest；pytest -n 8 tests", "是两个执行阶段。第一阶段仍预测约 1.22 秒；第二阶段与第 17 条相同，-n 8 tests 被折叠后退到 Python 根节点，整条预测由旧版 118.89 秒降到 2.61 秒。"),
    ("精确节点混合更具体调用", "python -m pytest -q（全量）", "所谓精确节点有 213 条覆盖样本，只有 73 条特征完全相同（中位数 4.84 秒），其余 140 条更具体测试命令的中位数 1.12 秒，混合后只有 1.47 秒。实际运行 970 个 pytest 测试及 9463 个 subtests，约 193 秒；相同特征组本身也仍有环境/任务差异。"),
    ("入口约束丢失", "pytest circuit_graph/... 多目录", "规则完整解析，Lattice 仍忽略 pytest 核心入口选择 Python 根节点；Edge 使用更具体父节点，约 83.82 秒。实际 5779 个测试通过，耗时 187.48 秒。"),
    ("全量测试分布过宽", "pytest tests -q -n 4", "tests 位于未知 -n 之前，因而被保留。Lattice 能命中 tests + quiet 的 54 条记录，中位数 82.03 秒，优于把 tests 放在 -n 后面的情况，但实际运行 31374 个通过测试，耗时 265.95 秒。"),
    ("精确节点混合更具体调用", "pytest -q -n auto", "精确特征节点共 18 条，其中相同完整特征只有 3 条，中位数 117.78 秒；其余 15 条更具体调用中位数 8.32 秒，混合后降到 43.53 秒。实际 pytest 自报 221.05 秒。"),
    ("全量与定向测试混合", "python -m unittest（成功）", "无测试参数的全量 unittest 与定向 unittest 共享子集节点。146 条记录中，64 条相同特征的中位数为 86.41 秒，82 条更具体调用中位数为 0.73 秒；混合后 2.10 秒。实际 Ran 1047 tests in 180.302s。"),
    ("全量与定向测试混合", "python3 -m unittest", "与第 23 条共享同一个 146 样本节点，预测 2.10 秒。实际 Ran 1051 tests in 168.547s。该问题在旧版完全相同。"),
    ("全量与定向测试混合", "python -m unittest（测试报错）", "与第 23 条共享节点。实际 Ran 1047 tests in 165.989s，89 个 errors；尽管最终报错，完整测试运行仍花了近三分钟，不能当作无效或零耗时标签。"),
    ("未知选项吞并目录", "pytest -n auto 多目录", "-n auto 之后的大范围测试路径成为一个 opaque 特征；Lattice/Edge 都失去可分别匹配的工作量信息，回退通用或混合测试历史。"),
    ("精确节点混合更具体调用", "pytest -q -n auto", "与第 22 条相同的 18 条覆盖记录，短的定向执行压低中位数。实际 14804 passed、2316 failed，pytest 自报 193.64 秒。"),
    ("大范围测试回退", "pytest circuit_graph/.../fourier", "语法完整，但未找到覆盖该目录组合的专门历史，使用与第 1 条相同的 94 条 pytest -q 混合记录，预测 5.33 秒。实际 3095 passed，pytest 自报 154.59 秒。"),
    ("未知选项吞并目录", "pytest -n auto ops measurements", "-n 连同测试目录合为 opaque；Lattice 回退 1.33 秒，Edge 回退 3.57 秒，再合成极短的 tail。实际 12911 passed，pytest 自报 141.77 秒。"),
    ("未知选项吞并目录", "pytest -n 4 circuit_graph/.../tape", "与第 14、17 条相同，未知 -n 使所有后续测试目录不可独立匹配；实际 16948 passed，pytest 自报 140.65 秒。"),
]

EXPECTED_RANKING = ["PL-3579:45", "PL-6107:26", "SQL-3630:41", "PL-3483:54", "SQL-3630:39", "PL-2947:35", "PL-3057:35", "PL-5277:42", "PL-3057:26", "PL-5860:29", "PL-5860:22", "PL-5860:25", "PL-3483:52", "PL-4993:45", "PL-3483:63", "PL-4161:69", "PL-3266:50", "PL-3266:61", "SQL-3515:28", "PL-3483:51", "PL-4993:51", "PL-4161:68", "SQL-3339:25", "SQL-3255:41", "SQL-3339:24", "PL-5277:36", "PL-2947:17", "PL-3579:52", "PL-5277:41", "PL-4993:41"]


def load(path):
    return json.loads(path.read_text(encoding="utf8"))


def dump(path, obj):
    data = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(gzip.compress(data, mtime=0) if path.suffix == ".gz" else data)


def main(source, output):
    output.mkdir(parents=True, exist_ok=True)
    index = load(source / "index.json")
    actual_ranking = [key.replace("swe-rebench:PennyLaneAI__pennylane-", "PL-")
                      .replace("swe-rebench:tobymao__sqlglot-", "SQL-")
                      for key in index["rankings"]["unique_lattice_edge"]]
    assert actual_ranking == EXPECTED_RANKING, "case annotations require the archived ranking"
    calls = {c["id"]: c for c in index["calls"]}
    evidence = {v: load(source / (v + "-evidence.json")) for v in ("phase1", "main")}
    evmap = {v: {c["id"]: c for c in e["calls"]} for v, e in evidence.items()}
    probes = load(source / "probes.json")
    sources = load(source / "training-sources.json")
    controls = load(source / "controls.json")
    controlmap = {c["id"]: c for c in controls}
    raw = {r["id"]: r for r in load(source / "raw-top30.json")}
    common = [c for c in calls.values() if c["models"]["lattice"]["main"]["p50"] is not None]
    summary = {"scope": "duration only; frozen task-held-out trace evaluation; no LLM",
               "split_sha256": index["split_sha256"], "labeled_calls": len(calls),
               "common_current_calls": len(common), "replayed_calls_per_version": len(evidence["main"]["calls"]),
               "replayed_duration_estimates": 2 * 2 * len(evidence["main"]["calls"]),
               "replayed_control_estimates": 2 * len(controls),
               "verified_node_duration_multisets": {v: sum(n["duration_multiset_matches"] for n in e["nodes"]) for v, e in evidence.items()},
               "training_source_verification": {k: sources[k] for k in ("train_tasks_verified", "wanted", "matched")},
               "models": {}}
    for model in ("tool", "trie", "lattice", "edge_kappa"):
        scored = [c for c in calls.values() if c["models"][model]["main"]["p50"] is not None]
        def error(c, version="main", model=model):
            return abs(c["models"][model][version]["p50"] - c["actual_ms"])
        paired = [c for c in scored if c["models"][model]["phase1"]["p50"] is not None]
        ranks = sorted(scored, key=error, reverse=True)
        transition = Counter((c["models"][model]["phase1"]["predicted_bucket"] == c["models"][model]["phase1"]["actual_bucket"],
                              c["models"][model]["main"]["predicted_bucket"] == c["models"][model]["main"]["actual_bucket"]) for c in paired)
        summary["models"][model] = {
            "scored": len(scored), "paired": len(paired),
            "current_mae_s": statistics.mean(error(c) for c in scored) / 1000,
            "common_current_mae_s": statistics.mean(error(c) for c in common) / 1000,
            "common_current_bucket_accuracy": statistics.mean(c["models"][model]["main"]["predicted_bucket"] == c["models"][model]["main"]["actual_bucket"] for c in common),
            "paired_old_mae_s": statistics.mean(error(c, "phase1") for c in paired) / 1000,
            "paired_new_mae_s": statistics.mean(error(c) for c in paired) / 1000,
            "bucket_right_to_wrong": transition[(True, False)], "bucket_wrong_to_right": transition[(False, True)],
            "top30_error_share": sum(error(c) for c in ranks[:30]) / sum(error(c) for c in scored),
            "top30_bucket_correct": sum(c["models"][model]["main"]["predicted_bucket"] == c["models"][model]["main"]["actual_bucket"] for c in ranks[:30]),
            "top30": [c["id"] for c in ranks[:30]],
            "new_coverage": [{"id": c["id"], "ae_s": error(c) / 1000} for c in scored if c not in paired],
        }
    worst = []
    for rank, (key, note) in enumerate(zip(index["rankings"]["unique_lattice_edge"], NOTES, strict=True), 1):
        c = calls[key]
        item = {**c, "rank": rank, "cause": note[0], "label": note[1], "explanation": note[2],
                "evidence": {v: evmap[v][key] for v in evmap}, "output_tail": raw[key]["output_tail"]}
        worst.append(item)
    summary["ranking"] = "30 unique calls ranked by max(abs(Lattice p50-actual), abs(Edge p50-actual)); separate top30 for every backend also provided"
    dump(output / "summary.json", summary)
    dump(output / "worst30.json.gz", worst)
    dump(output / "evidence.json.gz", {"selected_calls": [c for c in calls.values() if c["id"] in index["selected"]],
        "evidence": evidence, "controls": controls, "probes": probes, "rankings": index["rankings"]})
    dump(output / "training-sources.json.gz", sources)
    h = html.escape
    def short(key):
        return key.replace("swe-rebench:PennyLaneAI__pennylane-", "PL-").replace("swe-rebench:tobymao__sqlglot-", "SQL-")
    def seconds(value):
        return "不可用" if value is None else f"{value / 1000:.3f}"
    sections = []
    table = []
    for c in worst:
        m = c["models"]
        table.append(f'<tr><td><a href="#case-{c["rank"]}">{c["rank"]}</a></td><td>{h(short(c["id"]))}</td><td>{h(c["label"])}</td><td>{seconds(c["actual_ms"])}</td><td>{seconds(m["lattice"]["main"]["p50"])}</td><td>{seconds(m["edge_kappa"]["main"]["p50"])}</td><td>{h(c["cause"])}</td></tr>')
        cllines = []
        for j, cl in enumerate(c["evidence"]["main"]["clauses"]):
            if "lattice" not in cl:
                continue
            node = cl["lattice"]
            old = c["evidence"]["phase1"]["clauses"]
            oldnode = old[j].get("lattice") if j < len(old) else None
            edge = cl.get("edge", {})
            cllines.append(f'<p>子命令 {j}: <code>{h(str(cl["argv"]))}</code><br>解析：{h(str(cl.get("parsed", {}).get("status")))}；诊断：{h(str(cl.get("parsed", {}).get("diagnostics", [])))}<br>Lattice：n={node["n"]}，p50={seconds(node["p50"])} 秒；节点特征 <code>{h(str(node["selected_features"]))}</code><br>旧节点：<code>{h(str(oldnode))}</code><br>Edge：精确节点样本 {edge.get("observation_count", 0)}，父节点证据并集 {edge.get("evidence_union_count", 0)}。</p>')
        source_ref = f'{c["raw"]["file"]}:{c["raw"]["line"]}'
        sections.append(f'<details id="case-{c["rank"]}"><summary>{c["rank"]}. {h(short(c["id"]))} — {h(c["label"])}</summary><p>{h(c["explanation"])}</p><pre>{h(c["command"] or c["tool"])}</pre><p>来源：<code>{h(source_ref)}</code>；action_id：<code>{h(c["raw"]["action_id"])}</code>。调用序号是 loader 的从 0 开始索引。</p><p>旧版 Lattice / Edge：{seconds(m["lattice"]["phase1"]["p50"])} / {seconds(m["edge_kappa"]["phase1"]["p50"])} 秒。新版实际 / Lattice / Edge：{seconds(c["actual_ms"])} / {seconds(m["lattice"]["main"]["p50"])} / {seconds(m["edge_kappa"]["main"]["p50"])} 秒。</p>{"".join(cllines)}<details><summary>实际运行输出末尾</summary><pre>{h(c["output_tail"])}</pre></details></details>')
    metrics_rows = "".join(f'<tr><td>{model}</td><td>{data["common_current_bucket_accuracy"]:.2%}</td><td>{data["common_current_mae_s"]:.3f}</td><td>{data["bucket_right_to_wrong"]} / {data["bucket_wrong_to_right"]}</td><td>{data["top30_error_share"]:.2%}</td></tr>' for model, data in summary["models"].items())
    rankings = []
    for model in ("tool", "trie", "lattice", "edge_kappa"):
        rows = []
        for rank, key in enumerate(index["rankings"][model], 1):
            c = calls[key]
            value = c["models"][model]["main"]
            detail = controlmap[key][model] if model in {"tool", "trie"} else evmap["main"][key]["models"][model]
            context = str(detail["context"]) + "; n=" + str(detail["evidence_counts"])
            rows.append(f'<tr><td>{rank}</td><td>{h(short(key))}</td><td>{seconds(c["actual_ms"])}</td><td>{seconds(value["p50"])}</td><td>{h((c["command"] or c["tool"])[:250])}</td><td>{h(context)}</td></tr>')
        rankings.append(f'<details><summary>{model} 独立最坏 30 条</summary><table><tr><th>序号</th><th>调用</th><th>实际秒</th><th>预测秒</th><th>命令摘要</th><th>匹配证据与样本数</th></tr>{"".join(rows)}</table></details>')
    regressions = []
    for model in ("lattice", "edge_kappa"):
        rows = []
        for c in calls.values():
            a, b = (c["models"][model][v] for v in ("phase1", "main"))
            if a["p50"] is None or b["p50"] is None or a["predicted_bucket"] != a["actual_bucket"] or b["predicted_bucket"] == b["actual_bucket"]:
                continue
            diagnostics = sorted({d for cl in evmap["main"][c["id"]]["clauses"] for d in cl.get("parsed", {}).get("diagnostics", [])})
            rows.append(f'<tr><td>{h(short(c["id"]))}</td><td>{seconds(c["actual_ms"])}</td><td>{seconds(a["p50"])}</td><td>{seconds(b["p50"])}</td><td>{a["predicted_bucket"]}→{b["predicted_bucket"]}</td><td>{h(str(diagnostics))}</td><td>{h((c["command"] or "")[:220])}</td></tr>')
        regressions.append(f'<details><summary>{model} 所有“旧对新错”的调用</summary><table><tr><th>调用</th><th>实际秒</th><th>旧 p50 秒</th><th>新 p50 秒</th><th>概率众数分桶</th><th>当前解析诊断（相关证据，不等于独立因果归因）</th><th>命令</th></tr>{"".join(rows)}</table></details>')
    text = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>ClawTune 时间预测误差归因</title>
<style>body{font:16px/1.65 system-ui,sans-serif;max-width:1280px;margin:36px auto;padding:0 24px;color:#17212b}h1,h2{line-height:1.3}table{border-collapse:collapse;width:100%;font-size:14px;margin:18px 0}td,th{border:1px solid #ccd3da;padding:7px;vertical-align:top}th{background:#edf2f7;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px}code{overflow-wrap:anywhere}details{border:1px solid #ccd3da;border-radius:6px;padding:12px;margin:12px 0}summary{cursor:pointer;font-weight:650}.note{background:#fff4da;padding:16px}a{color:#145cb2}</style>
<h1>ClawTune 时间预测误差归因</h1>
<p>结论：当前问题同时存在于规则覆盖、特征表达、节点查询约束、子集统计汇总和运行状态信息。不能把解析完整性当成预测准确性，也不能把所有当前大误差归因于本次更新。</p>
<p>使用原来的 515 训练任务 / 116 测试任务划分。仅分析 duration_ms，冻结 KB，不调用 LLM、不执行 trace 中的命令、不修改数据集或 release seeds。独立重放新旧各 132 个诊断调用，两个后端共 528 个 duration p50 结果与归档一致（含不可用状态）。选中节点的训练耗时多重集合也逐一核对。</p>
<p>评分实际值沿用 offline loader：exec 使用合格的 retained clause workload duration，不直接用 tool hook 总耗时。非 exec 使用 loader 的相应调用耗时。下面主表的 1216 个共同调用都来自 PennyLane（461）和 SQLGlot（755），不是覆盖全部仓库的命令准确性结论。</p>
<h2>当前准确性与版本退步</h2>
<p>本表准确率/MAE在当前四个后端均可预测的同一批 1216 个调用上计算；旧对新错的转换按新旧各后端共同可预测集合计算（Lattice/Edge 为 1205）。最坏30误差占比按各后端全部可预测调用计算，不能横向误读为同一分母。</p>
<table><tr><th>后端</th><th>共同样本分桶准确率</th><th>共同样本 MAE 秒</th><th>旧对新错 / 旧错新对</th><th>自身最坏30绝对误差占比</th></tr>METRICS</table>
<p>分桶边界为 0.1、0.5、2、10 秒；预测桶取概率最大项，不是把 p50 再分桶。超过 10 秒没有进一步区分。Lattice 最坏30只有9条分桶正确；Edge 最坏30有21条分桶正确，仍存在数百秒误差。因此必须同时看连续误差。</p>
<p>Lattice 共同样本净损失24次分桶命中（32退步、8改善）；32条退步中14条出现 unknown_option:-n。Edge 净损失16次（31退步、15改善），缺失规则、未知选项、父节点证据变化和边界附近预测共同出现。逐条诊断见文末表，诊断计数允许重叠。</p>
<p>Lattice 共同样本 MAE 13.244→13.501 秒，Edge 12.960→12.974 秒。新增11条覆盖各增加约1387秒总绝对误差，使全可预测集合的 MAE 进一步升到14.519/13.997秒；其中 cargo fmt 和 make/Rust构建两条贡献约984/981秒。新增可预测并不保证新增预测可信。</p>
<p>Tool/Trie也存在长尾问题，已另行重放各132个诊断调用的264项结果。Tool最坏30中26条使用命令前缀回退，4条是exact_command；前缀通常停在python -m pytest、pytest -q或pip install，无法区分后续测试范围，也可能忽略复合命令的后半段。Trie同样受前缀/程序级回退及无条件合成影响。比如SQL-3799:29包含定向测试、全量pytest和make test，实际207.10秒，Tool仅按前4个词的9条历史预测0.033秒；SQL-3630:41中Trie也把唯一cargo test记录借给cargo fmt。二者此次未变化，不代表不存在问题。</p>
<h2>已验证的机制</h2>
<ol>
<li><b>未知选项连带损失测试目录。</b>pytest -n 未预置，遇到它后整个剩余 argv 进入 opaque。旧版 heuristic 碰巧把 -n 当带值选项，因此 tests/devices 可以独立匹配。5277:42 的旧预测383.23秒、新预测1.36秒，实际368.89秒，是明确的退步例子。保守停止是避免错误猜测的策略，但在规则缺口大时会损失预测信息。</li>
<li><b>核心入口未在查询时强制保留。</b>core 约束了建节点；查询只要求节点特征是 query 的子集。来自其他 Python 执行方式的 tool=python 节点仍可被选中。主榜30条中15条涉及此节点，历史3193条、中位数1.333秒；其中10条旧版也已命中该根节点。缩减方差风险给此节点的分数2.413低于pytest专门节点约3.17–3.26。仅在诊断查询中强制保留 core，可把代表病例从1.333秒改到3.568秒，仍远低于数百秒，说明不能只修这一处。</li>
<li><b>“精确节点”统计包含更具体命令。</b>subset coverage 汇总所有包含节点特征的训练命令。无位置参数不是一个显式特征，导致“全量运行”和“指定一个测试”共用父节点。unittest 的64条相同特征记录中位数86.408秒，被82条更具体记录（中位数0.726秒）拉到2.103秒。精确命中 shortcut 直接使用这个已经混合的统计；此现象两后端、两版本都存在。</li>
<li><b>工作量和运行状态仍不足。</b>pytest目录名没有表达测试数量、参数化规模；同一个repo含不同task对应版本，解释器路径被归一化，依赖、缓存、last-failed状态没有进入本次离线特征。证据包括同形 pytest -q tests 只有33.50/51.89秒两条训练记录，却出现390–415秒执行；这里不能断言究竟是哪个环境因素造成差异。</li>
<li><b>未知工具和条件执行叠加。</b>cargo fmt只借到了唯一cargo test的538.52秒记录，而真实环境没有rustfmt。make install-dev-rs缺乏专门历史，实际Rust构建575.34秒，合成器仍把未执行的test-rs计入。当前串行合成假设保留阶段都会执行，不能预测失败导致的短路。</li>
</ol>
<p class="note">主榜30条中：Lattice 18条预测与旧版不变、10条误差增加、2条为新增预测；Edge 为11条不变、16条变差、1条改善、2条新增。这说明当前最坏问题相当一部分早已存在。LLM 在本次离线协议中禁用；没有测量动态生成规则的收益。</p>
<h2>最坏30条：逐调用去重榜</h2>
<p>排序为 max(|Lattice p50−实际|, |Edge p50−实际|)，从大到小，单位秒。PL=PennyLane；SQL=SQLGlot。冒号后为调用索引。该榜与 Lattice 单独前30相同；Edge 单独前30有一条替换，详见附表。点击序号后展开相应详情，可查看完整命令、来源行号、旧/新节点和运行输出。</p>
<table><tr><th>#</th><th>任务:调用</th><th>执行摘要</th><th>实际秒</th><th>Lattice秒</th><th>Edge秒</th><th>主要机制</th></tr>WORST</table>
DETAILS
<h2>按后端独立排名和退步明细</h2>
RANKINGS
REGRESSIONS
<h2>后续优先级与边界</h2>
<p>优先补齐评估中实际遇到的语法（pytest插件选项、cargo子命令等），确认 core 的查询约束；接着分别验证“完整特征精确历史”和“子集覆盖历史”的使用方式，以及全量/定向测试、位置参数数量和测试范围的表达。环境状态与条件执行属于后续建模问题。以上措施应逐项做固定划分消融，不能把诊断样本上的改善当作独立验证，更不能承诺补规则就会解决所有长尾误差。</p>
<p>当前工作只新增分析脚本和证据，不修改生产解析器、KB算法、seeds或数据集。所有耗时使用原始观测，没有给缺失标签补零；真实测试失败、编译失败也可能耗时很长，不能一概删除。</p>
<p>机器可读文件：<a href="summary.json">汇总</a>、<a href="worst30.json.gz">30条完整记录</a>、<a href="evidence.json.gz">新旧重放、节点与诊断探针</a>、<a href="training-sources.json.gz">训练样本来源</a>。完整命令的证据以这些JSON为准。</p>
</html>'''
    text = text.replace("METRICS", metrics_rows).replace("WORST", "".join(table)).replace("DETAILS", "".join(sections)).replace("RANKINGS", "".join(rankings)).replace("REGRESSIONS", "".join(regressions))
    (output / "report.html").write_text(text, encoding="utf8")
    # This is a standalone local report; all source-derived strings are escaped.
    assert len(re.findall(r'id="case-\d+"', text)) == 30
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    main(args.source, args.output)
