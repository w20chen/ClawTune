# Legacy 数据集预测算法实验：复现步骤

本文档说明如何复现"用 ClawTune 的预测算法在外部 legacy trace 数据集上做 80/20
训练/测试评估"的实验。评估逻辑由独立模块 `legacy_eval/` 实现，不改动任何算法代码。

## 1. 前置条件

- Windows 或 Linux 均可（本实验不需要 Docker、cgroup、eBPF）。
- Python 3.12 + `numpy`（`tool_resource`/`tool_time` 的依赖）。
- legacy 数据集目录：`D:\swe100-full-5be74da-20260726`（100 个任务，每个含
  `attempt_1/clause_telemetry.json` + `trace.jsonl`）。
- 在仓库根目录运行命令（`legacy_eval/_bootstrap.py` 会自动把
  `services/scheduler/src` 加入 `sys.path`，无需设置 `PYTHONPATH`）。

## 2. 复现完整实验（seed=42，80/20）

在仓库根目录执行：

```bash
python -m legacy_eval --dataset D:\swe100-full-5be74da-20260726 --print-summary
```

`--train-frac` 默认 `0.8`、`--seed` 默认 `42`，所以这条命令等价于：
`python -m legacy_eval --dataset D:\swe100-full-5be74da-20260726 --train-frac 0.8 --seed 42`。

运行会：

1. 加载全部 100 个任务；
2. 按任务随机划分 **80 训练 / 20 测试**（seed=42，确定性）；
3. 仅用训练集构建 `ClauseResourceKB`、`LatticeTimeKB`、`RuntimeToolResourceKB`；
4. 回放测试集，在每个工具调用前记录一次预测（纯静态协议：测试数据不进入 KB）；
5. 输出各算法指标（JSON + Markdown）。

## 3. 输出位置

默认写入 `legacy_eval/.runtime/<时间戳>/`（该目录已在 `.gitignore` 中忽略）：

- `report.json` —— 完整结果：划分、数据统计、逐调用预测记录、各算法汇总指标；
- `report.md` —— 人类可读的指标摘要。

也可以用 `--out` / `--markdown` 指定输出路径。

## 4. 验证

### 单元测试

```bash
python -m pytest tests/test_legacy_eval.py -q --basetemp .pytest-tmp-root
# 预期：13 passed
```

### 结果核对（seed=42 全量运行，应能复现）

数据统计：

| 项 | 值 |
| --- | --- |
| 训练 / 测试任务数 | 80 / 20 |
| 训练 clause 观测（全部 / eligible） | 3662 / 3643 |
| 训练成功工具调用 | 3686 |
| 测试 clause 事件 | 828 |
| 测试工具调用 | 935 |

各算法指标（与 `report.md` 对齐即可，数值允许小数点后几位浮动）：

| 算法 | 样本数 | 覆盖率 | 关键指标 |
| --- | --- | --- | --- |
| `clause_latency_bucket` | 828 | 100% | top-1 准确率 ≈ 66.4%，Brier ≈ 0.416 |
| `shrinkage` | 828 | 100% | MAE ≈ 1169.8ms，中位误差 ≈ 13.7ms |
| `loso` | 828 | 100% | MAE ≈ 1166.5ms，中位误差 ≈ 15.3ms |
| `max_cardinality` | 828 | 100% | MAE ≈ 1207.0ms，中位误差 ≈ 14.5ms |
| `continuous_latency_p90` | 935 | 100% | pinball(q=0.9) ≈ 657.3 |
| `continuous_cpu_p90` | 935 | ≈ 12% | pinball(q=0.9) ≈ 0.214 |

已知限制（会如实出现在报告的 Notes 中，非故障）：

- `peak_memory_mb` 不评估：legacy 数据没有 ambient 内存锚点；
- 连续 CPU 覆盖率低：短 clause（<1s）没有可用的 `peak_cpu_cores`；
- clause 行无时间戳：静态协议下用合成时间戳满足因果护栏。

## 5. 常用变体

```bash
# 换随机种子（划分不同）
python -m legacy_eval --seed 7 --print-summary

# 冒烟测试（少量任务，快速验证管道）
python -m legacy_eval --max-train-tasks 4 --max-test-tasks 2 --print-summary

# 自定义输出位置与延迟桶边界（ms）
python -m legacy_eval --out legacy_eval/.runtime/report.json \
  --bucket-edges 100,500,2000,10000
```

## 6. 导出为项目 cold-start KB（替换 traces/tool-resource）

把 80 个训练任务训练出的 KB 序列化到项目的 cold-start 种子目录
`traces/tool-resource/`（运行时 sidecar 会作为冷启动加载）：

```bash
# 导出并直接覆盖项目种子（80 训练任务 / seed=42；--skip-eval 只导出不评估）
python -m legacy_eval --export-kb traces/tool-resource --skip-eval

# 先导出到暂存目录审查，确认后再覆盖
python -m legacy_eval --export-kb legacy_eval/.runtime/coldstart --skip-eval
```

导出 3 个快照（与项目校验器要求的 schema 一致）：

| 文件 | schema |
| --- | --- |
| `clause-resource-kb.json` | `runtime_clause_resource_kb_v4` |
| `clause-lattice-time-kb.json` | `clause_lattice_time_kb_v1` |
| `runtime-tool-resource-kb.json` | `runtime_tool_resource_kb_v1` |

设计约定：

- **来源**：seed=42 划分的 80 个训练任务（与评估报告同一套训练数据）；
- **仅公共层**：per-repo 层导出为空（80 个 legacy 任务 repo 对项目新任务无意义）；
- **内存先验**：legacy 无 ambient 内存锚点，导出时合并保留现有
  `traces/tool-resource/runtime-tool-resource-kb.json` 的 `peak_memory_mb`
  global 节点；
- 导出文件会被项目的 `_validate_kb_snapshot_pair` 校验通过（已验证）。

## 7. 相关文件

- 评估模块与库用法：`legacy_eval/README.md`
- 单元测试：`tests/test_legacy_eval.py`、`tests/test_legacy_eval_export.py`
- 冷启动导出实现：`legacy_eval/export.py`
