"""Evidence-backed, post-evaluation explanations. Never used by predictors."""
import gzip
import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent
rows = json.loads(gzip.decompress((OUT/'worst30-evidence.json.gz').read_bytes()))
notes = {
1: ('测试范围或配置在回退时丢失', 'pytest：returntypes/tape/transforms', '三个测试目录均被回退丢弃，使用 repo+pytest+-q 的 94 条记录，54 条不足 10 秒，中位数 5.33 秒；本次输出显示 5695 passed、1 failed，实际运行约 598 秒。'),
2: ('测试范围或配置在回退时丢失', 'pytest：returntypes，禁用 asyncio 插件', '回退丢弃 tests/returntypes 和 -p no:asyncio，使用 642 条混合记录，406 条不足 10 秒；本次实际执行 4139 passed、14 failed，耗时 481 秒。'),
3: ('构建目标被泛化成其他工作负载', '安装 maturin → Rust 构建 → 测试', 'make install-dev-rs 的目标参数被丢弃，使用 90 条 make 历史，其中 85 条是 make test；构建子命令预测中位数 7.14 秒，实测 575.34 秒，最终因 Rust E0658 编译失败。还预测了实际未执行的 make test-rs，该项反而部分抵消了低估。'),
4: ('测试范围或配置在回退时丢失', 'pytest tests，排除一个用例', '选择 tests+--tb=short 的 32 条历史，混合不同并行和筛选配置，中位数 56.07 秒；--no-flaky-report 之后的参数作为未解析整体保留，随后在选择时丢弃。本次实际有 14531 passed、1139 failed。'),
5: ('完整相同特征的样本过少且不代表本次', 'pytest tests，限制数值库线程', '完整特征仅有 2 条训练样本：33.50 秒收集失败、51.89 秒执行约 6357 个测试；本次执行 16288 个通过用例并耗时 415.08 秒。当前特征也没有区分这些显式线程环境变量。'),
6: ('完整相同特征的样本过少且不代表本次', 'pytest tests', '与第 5 项使用同样 2 条历史，直接采用 42.70 秒中位数；本次实际执行 16225 passed、63 failed，耗时 389.69 秒。'),
7: ('测试范围或配置在回退时丢失', '并行 pytest tests/devices；打印日志', '回退丢弃 tests/devices，选中的 5 条 pytest 记录全部来自另一个任务 pennylane-2603，范围和过滤配置不同；预测 95.85 秒，本次 4210 passed、1776 skipped，实测 368.89 秒。tail 的误差可以忽略。'),
8: ('没有同配置样本而混用额外参数样本', 'pytest tests/gradients', '节点特征虽然等于查询，但 own_durations 为空；实际使用 9 条带额外参数或测试目录的记录，其中 8 条显式设置 -n。中位数 113.44 秒，本次未显式设置 -n，2372 passed，实测 348.59 秒。'),
9: ('没有同配置样本而混用额外参数样本', 'pytest tests/gradients', '与第 8 项使用同样的 9 条非同配置历史；本次 2324 passed、48 failed，实测 348.36 秒。测试确实执行，不能当成启动即失败。'),
10: ('没有同配置样本而混用额外参数样本', 'pytest tests/gradients', '与第 8 项使用同样的 9 条非同配置历史；本次 2354 passed、18 failed，实测 345.67 秒。'),
11: ('测试范围或配置在回退时丢失', 'pytest --lf -x -vv --tb=long', '选择时丢弃 --last-failed，使用 4 条其他测试范围的短记录（1.77–2.31 秒）；本次实际运行 631 个通过用例和重试，直到出现错误才停止。之前失败用例的缓存内容没有表示。'),
12: ('同命令混合提前失败与实际执行', 'SQLGlot pytest -q', '完整特征的 73 条历史中，36 条对应调用输出被规则识别为收集失败，38 条不足 10 秒，中位数 4.84 秒；本次确实执行 881 passed、89 failed，并出现 PySpark 解释器版本不一致，耗时 194.67 秒。'),
13: ('测试范围或配置在回退时丢失', '4 进程测试 data/fermi/kernels 等', '仅保留第三个位置的 tests/kernels 等少数特征，丢弃其他四个测试目录和 -n 4，使用同一训练任务的 2 次不同目录组合、-n auto 执行。预测 90.41 秒，本次 1277 passed、391 failed、8 errors，实测 268.05 秒。'),
14: ('测试范围或配置在回退时丢失', '测试 numpy/ops 等十个目录', '只采用 1 条训练记录：另一组八个目录、-n 8，耗时 95.66 秒；本次十个目录且未显式设置 -n，10037 passed，耗时 271.62 秒。选中节点还丢弃了后六个目录和插件参数。'),
15: ('测试范围或配置在回退时丢失', '测试 templates/transforms', '选择只保留第二个位置 tests/transforms 的 2 条记录，两条均显式并行（-n auto、-n 6）且包含不同目录；本次 2831 passed，耗时 274.04 秒。'),
16: ('测试范围或配置在回退时丢失', '4 进程执行全部 tests', '回退丢弃 tests，保留 -n 4，使用 8 条不同范围的记录，中位数 122.13 秒；本次执行 31374 passed、14375 skipped，实际 269.68 秒。'),
17: ('条件命令中未执行阶段仍被计入预测', '装 venv → 装 maturin → 构建 → 测试', '构建阶段报 Unable to find maturin script 后退出，make test-rs 没有执行；预测仍计入该阶段约 112 秒的历史耗时，将实际 7.80 秒估成 154.71 秒。'),
18: ('条件命令中未执行阶段仍被计入预测', '创建 venv → 安装 → 构建 → 测试', '首个 venv 命令因 ensurepip 不可用，在 0.0965 秒结束；后面的 pip、构建和测试均未执行，但它们仍参与组合预测，得到 142.57 秒。'),
19: ('同命令混合提前失败与实际执行', '禁用 Rust tokenizer，pytest', '完整特征 87 条历史中，47 条对应输出被识别为收集失败，且均不足 10 秒，将中位数压到 1.73 秒；本次 596 passed、2 skipped，实测 137.62 秒。'),
20: ('同命令混合提前失败与实际执行', '调整 Python 路径后 make test', 'make test 的 85 条历史混合命令找不到等失败与正常运行，50 条不足 10 秒，中位数 7.13 秒；本次通过 PATH 指向可用解释器，实际运行 661 个测试约 139 秒。'),
21: ('同命令混合提前失败与实际执行', '指定 PATH 后 make test', '与第 20 项使用同一组 85 条 make test 记录；本次实际运行 748 个测试约 138 秒。基础特征没有表达 PATH 所选择的执行环境。'),
22: ('同命令混合提前失败与实际执行', '禁用 Rust tokenizer，pytest -q', '与第 12 项共享 73 条含收集失败的完整特征历史；本次实际执行 567 passed，耗时 135.54 秒，仍预测 4.84 秒。'),
23: ('同命令混合提前失败与实际执行', 'SQLGlot pytest -q', '与第 12 项共享 73 条含收集失败的历史；本次 560 passed、1 failed，耗时 131.51 秒。断言失败发生在实际执行之后。'),
24: ('完整相同特征的样本过少且不代表本次', 'pytest tests，中途进程崩溃', '与第 5 项共享仅 2 条历史，预测 42.70 秒；本次运行到 167.89 秒后 Aborted (core dumped)，退出码 134。这是到崩溃为止的已观测耗时，不是成功完成全部测试的耗时。'),
25: ('同命令混合提前失败与实际执行', 'SQLGlot pytest -q', '与第 12 项共享 73 条含收集失败的历史；本次 638 passed、2 skipped，实际 129.65 秒，预测 4.84 秒。'),
26: ('同命令混合提前失败与实际执行', 'SQLGlot pytest -q', '与第 12 项共享 73 条含收集失败的历史；本次 613 passed、2 skipped，实际 128.33 秒，预测 4.84 秒。'),
27: ('同命令混合提前失败与实际执行', 'SQLGlot pytest', '与第 19 项共享 87 条历史，其中 47 条对应收集失败；本次 554 passed，实际 120.83 秒，预测 1.73 秒。'),
28: ('测试范围或配置在回退时丢失', '4 进程测试 docs/drawer 等小范围', '与第 16 项选中完全相同的 8 条 -n 4 历史，所有测试目录被丢弃，因此同样预测 122.13 秒；本次只有 639 passed、90 skipped，实际 7.09 秒，是正常完成而非提前失败。'),
29: ('条件命令中未执行阶段仍被计入预测', '装 Rust → 构建 → 测试', 'maturin 因未找到 virtualenv/conda 环境而失败，make test-rs 没有执行；预测仍计入该阶段约 112 秒历史，实际 15.62 秒却预测 130.46 秒。'),
30: ('同命令混合提前失败与实际执行', 'SQLGlot pytest -q', '与第 12 项共享 73 条含收集失败的历史；本次 561 passed，实际 118.62 秒，预测 4.84 秒。'),
}
review={}
for r in rows:
    category,command,reason=notes[r['rank']]
    output=str(r['raw_data'].get('tool_result',''))
    lines=[l for l in output.splitlines() if re.search(r'\d+ (?:passed|failed|errors?|skipped).*\bin |^Ran \d+ tests? in|Exit code:|ensurepip|Unable to find|Couldn.t find a virtualenv|error\[E0658\]|Aborted|PYTHON_VERSION_MISMATCH',l)]
    review[r['id']]={'category':category,'command_summary':command,'reason':reason,
        'output_evidence':'\n'.join(lines[-12:]) if lines else output[-1400:],
        'limitation':'已确认模型使用的样本及实际输出；执行速度差异还可能受代码版本、硬件、依赖和缓存影响，本次未通过受控执行分离这些因素的因果贡献。'}
(OUT/'review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
assert len(review)==30
