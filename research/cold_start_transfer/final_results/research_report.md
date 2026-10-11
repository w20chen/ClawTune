# ClawTune 冷启动知识迁移可行性研究

**研究决策：INCONCLUSIVE。** 已完成真实 trace 审计、E1 描述性分析、E2 仓库留出/少样本评估及 E3 原始 KB 消融。现有样本可以量化一批 QEMU 环境下的冷启动行为，但不足以支持“可迁移的参数效应”或面向准入内存的新语义迁移算法。这个结论不等于证明路线无效。

## 范围与证据

代码基线 `8fce6b72770b2eaf8a973b82183c72477fb94c2f`；随机种子 20261011。生产代码、OpenClaw core、原始 trace、训练集与既有 KB 均未修改。来源和质量见 `data_audit.md`，机制见 `implementation_audit.md`，切分和时序见 `leakage_audit.md`。全部实验为 clause-level；n 为目标指标有效 clause 观测数。模拟串行 replay 不等于真实在线事件重放。

## RQ1：是否存在跨仓库规律？

主 SWE 批次有 11 个独立仓库、30 个任务、778 条有效 duration/RSS clause；其中 24 个语法命令族在至少两个仓库出现：`{"cat": 9, "find": 9, "git:branch": 4, "git:diff": 10, "git:log": 10, "git:merge-base": 2, "git:show": 5, "git:stash": 6, "git:status": 4, "grep": 11, "head": 2, "ls": 11, "pip:install": 5, "pip:list": 3, "pip:show": 3, "python:inline": 10, "python:interpreter": 3, "python:module:pytest": 10, "python:script": 3, "rm": 3, "sed": 7, "timeout": 2, "which": 3, "xargs": 3}`。这建立了可比较的观察性数据覆盖，不自动证明参数规律。

各仓库资源分布在 `e1_transferability.csv`，仓库中位数的跨仓库离散度和仓库 bootstrap 区间在 `e1_between_repository.csv`；真实相同 task+argv 的重复噪声在 `e1_repeat_noise.csv`。重复执行仍可能夹杂代码修改与缓存变化，不能作为纯测量噪声。直接 pytest 与 python-module pytest 扫描共发现 13 组候选命令对、26 条目标指标差值；分别保存在 `e1_parameter_pairs.csv` 与 `e1_module_parameter_pairs.csv`。仅在同任务、相同剩余 argv、单一 workers/coverage 类别变化时保留。**严格验证输入规模、代码状态和运行条件一致的参数匹配对为 0。** 因而相对变化是否比绝对量稳定、哪些参数可迁移或造成负迁移，当前无法可靠回答，不报告虚构的 delta 分布或因果效应。`command_inventory.csv` 保留 executable、语法 family、规范化模板、选项及样本/仓库数，输入规模缺失明确标为 unrecorded。

代表命令族的绝对尺度与重复离散度如下（均为主批次 duration；log 指 log1p(duration/1000ms)）：

| Family | Repos | Clauses | Repo median range (ms) | Between-repo log SD | Within-task repeat log SD (repo mean) | Repeat repos |
|---|---|---|---|---|---|---|
| cat | 9 | 76 | 32.8–52.4 | 0.006113 | 0.0003933 | 1 |
| ls | 11 | 80 | 39.2–55.3 | 0.006102 | 0.0005253 | 2 |
| grep | 11 | 77 | 49.1–76.8 | 0.008988 | — | 0 |
| git:log | 10 | 84 | 73.0–219.5 | 0.03918 | 0.0571 | 4 |
| python:inline | 10 | 98 | 757.8–83960.8 | 1.242 | 0.3516 | 2 |
| python:module:pytest | 10 | 91 | 14984.3–108939.4 | 0.6363 | 0.07631 | 7 |

`cat`、`ls`、`grep` 在本批次的绝对时间分布较集中；Python inline 和 pytest 的仓库尺度差异明显，因而单一 executable 先验容易错配。不过短调用在 log1p 下也会被压缩，不能把较小 SD 直接解释为可迁移的参数效应；共享 QEMU/监测开销也未被独立分离。重复列只统计同 task+argv 的真实重复，先在仓库内平均再按仓库等权，包含代码/缓存变化，不是受控噪声下界。相对参数变化缺少合格匹配对，所以不能完成绝对量与参数 delta 稳定性的有效比较。对这些命令族的预测迁移证据仍须结合下述严格留出结果。

## RQ2：零样本与少量本地观测是否有效？

主批次 duration 零样本结果如下。availability 与误差均按仓库等权平均；各自误差的预测覆盖集合不同，真正的成对改进看最后两列（candidate minus Lattice，负数更好）。P50 误差就是 MAE/log MAE；完整原单位 MAE 和 p90 pinball 在 CSV。

| Method | Availability | Log MAE | P90 coverage | Paired delta vs Lattice | 95% repo bootstrap |
|---|---|---|---|---|---|
| global | 1 | 0.8587 | 0.9347 | 0.3504 | [0.02236, 0.6126] |
| executable | 0.9859 | 0.4809 | 0.8517 | -0.05384 | [-0.1272, 0.02019] |
| family | 0.971 | 0.354 | 0.8385 | -0.1697 | [-0.3231, -0.03232] |
| trie | 0.9798 | 0.4607 | 0.8523 | -0.05257 | [-0.1259, 0.021] |
| lattice | 0.97 | 0.5178 | 0.8416 | 0 | [0, 0] |
| lattice_no_repo | 0.97 | 0.5184 | 0.8398 | 0.0005969 | [0, 0.001791] |
| lattice_loso | 0.97 | 0.521 | 0.8516 | 0.003156 | [-0.002488, 0.01205] |
| edge | 0.97 | 0.5134 | 0.8373 | -0.004375 | [-0.0101, 0.0008882] |
| edge_fixed | 0.97 | 0.5134 | 0.8373 | -0.004375 | [-0.0101, 0.0008882] |

下表学习曲线固定为能支持 n=20 的相同仓库和相同测试执行集合；这避免曲线随 n 更换仓库或测试样本。其他仓库支持的全部 n 仍在 `e2_cold_start.csv`，没有补齐不存在的历史。

| Method | n | Same repositories | Log MAE | Availability |
|---|---|---|---|---|
| family | 0 | 4 | 0.4652 | 0.9685 |
| family_calibrated | 0 | 4 | 0.4652 | 0.9685 |
| lattice | 0 | 4 | 0.6079 | 0.9787 |
| edge | 0 | 4 | 0.6076 | 0.9787 |
| edge_fixed | 0 | 4 | 0.6076 | 0.9787 |
| edge_freeze_target | 0 | 4 | 0.6076 | 0.9787 |
| family | 1 | 4 | 0.4652 | 0.9685 |
| family_calibrated | 1 | 4 | 0.4653 | 0.9685 |
| lattice | 1 | 4 | 0.6079 | 0.9787 |
| edge | 1 | 4 | 0.6076 | 0.9787 |
| edge_fixed | 1 | 4 | 0.6076 | 0.9787 |
| edge_freeze_target | 1 | 4 | 0.6076 | 0.9787 |
| family | 2 | 4 | 0.4652 | 0.9685 |
| family_calibrated | 2 | 4 | 0.4654 | 0.9685 |
| lattice | 2 | 4 | 0.6088 | 0.9787 |
| edge | 2 | 4 | 0.6076 | 0.9787 |
| edge_fixed | 2 | 4 | 0.6076 | 0.9787 |
| edge_freeze_target | 2 | 4 | 0.6076 | 0.9787 |
| family | 5 | 4 | 0.4652 | 0.9685 |
| family_calibrated | 5 | 4 | 0.4591 | 0.9685 |
| lattice | 5 | 4 | 0.6022 | 0.9787 |
| edge | 5 | 4 | 0.6013 | 0.9787 |
| edge_fixed | 5 | 4 | 0.6032 | 0.9787 |
| edge_freeze_target | 5 | 4 | 0.6032 | 0.9787 |
| family | 10 | 4 | 0.4652 | 0.9685 |
| family_calibrated | 10 | 4 | 0.4623 | 0.9685 |
| lattice | 10 | 4 | 0.6029 | 0.9787 |
| edge | 10 | 4 | 0.6012 | 0.9787 |
| edge_fixed | 10 | 4 | 0.6031 | 0.9787 |
| edge_freeze_target | 10 | 4 | 0.6031 | 0.9787 |
| family | 20 | 4 | 0.4652 | 0.9685 |
| family_calibrated | 20 | 4 | 0.2599 | 0.9685 |
| lattice | 20 | 4 | 0.5281 | 0.9787 |
| edge | 20 | 4 | 0.5526 | 0.9787 |
| edge_fixed | 20 | 4 | 0.5481 | 0.9787 |
| edge_freeze_target | 20 | 4 | 0.5481 | 0.9787 |

固定 cohort 下，校准法 log MAE 在 n=0/1/20 分别为 **0.4652 / 0.4653 / 0.2599**；Lattice 对应 **0.6079 / 0.6079 / 0.5281**。这直接回答了本批次少样本是否改善，不能外推成所有仓库的收敛保证。Edge 在 n=20 的 learned / fully-fixed / target-frozen 分别为 0.5526 / 0.5481 / 0.5481。

校准相对同一 source-family 先验的仓库级收益与负迁移：

| n | Repos | Improved | Worsened | Mean delta vs family | Worst repo delta |
|---|---|---|---|---|---|
| 1 | 5 | 3 | 2 | 0.0007492 | 0.003568 |
| 2 | 5 | 2 | 3 | 0.0008037 | 0.003242 |
| 5 | 5 | 3 | 2 | -0.004204 | 0.003525 |
| 10 | 5 | 4 | 1 | -0.008298 | 0.01369 |
| 20 | 4 | 3 | 1 | -0.2053 | 0.0002811 |

n=20 的平均改善中，15five/scim2-filter-parser 贡献约 86.7%；这个集中度进一步限制外推。 另外，`armmbed/mbed-tools` 在 n=20 的 P50 log MAE 改善，但 P90 pinball 反而增加约 4173 ms；中位数改善不等于尾部预测更可靠。

`calibration_effect.csv` 给出校准相对未校准 family 先验的仓库级变化、真正获得同 family 反馈的测试样本数。一个本地样本只修正兼容命令族，没有证据时偏差仍为 0；它不能证明修正了整个仓库所有工具。`paired_repository_bootstrap.csv` 报告相对 Lattice 的负迁移仓库比例和最差退化幅度。小样本或宽区间不支持“预测已稳定”的结论；n 的上限 20 也不等于保证收敛。

准入相关环境增量内存共 38 条，仅 2 个独立仓库，测量口径和采集 run 分开：

另有 19 条旧 clause 内存虽标记 eligible，但被测 cgroup 不包含执行进程，已按原始路径证据隔离；不能用这些零值推导准确率。工具级另隔离 23 条，明细见 data audit 与 quarantine 文件。

| Condition | n | Method | Repos | Availability | P90 pinball (MiB) | P90 coverage |
|---|---|---|---|---|---|---|
| v6:trace-audit:20260920-172943-8ea96fce / aarch64 / None / 320.0 / qemu / 2 / cgroup_v2_environment_union_v1 | 0 | family | 2 | 0.6429 | 0.2274 | 0.75 |
| v6:trace-audit:20260920-172943-8ea96fce / aarch64 / None / 320.0 / qemu / 2 / cgroup_v2_environment_union_v1 | 0 | family_calibrated | 2 | 0.6429 | 0.2274 | 0.75 |
| v6:trace-audit:20260920-172943-8ea96fce / aarch64 / None / 320.0 / qemu / 2 / cgroup_v2_environment_union_v1 | 0 | lattice | 2 | 0.6429 | 1.786 | 0.5 |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 0 | family | 2 | 0.4842 | 2.772 | 0.619 |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 0 | family_calibrated | 2 | 0.4842 | 2.772 | 0.619 |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 0 | lattice | 2 | 0.4842 | 2.756 | 0.619 |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 1 | family | 1 | 0.3684 | 4.448 | 0.5714 |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 1 | family_calibrated | 1 | 0 | — | — |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 1 | lattice | 1 | 0.3684 | 4.782 | 0.5714 |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 2 | family | 1 | 0.3684 | 4.448 | 0.5714 |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 2 | family_calibrated | 1 | 0 | — | — |
| v6:trace-audits:20260921-041901-94a4b095 / aarch64 / None / 320.0 / qemu / None / cgroup_v2_environment_union_v1 | 2 | lattice | 1 | 0.3684 | 4.782 | 0.5714 |

同一对仓库的多个批次不是独立重复仓库。多数批次每仓库仅一任务，只能支持零样本；少样本结果只能视为病例描述。RSS 单独报告，不能补足环境内存证据。不能由这些数据判断时间与准入内存是否具有相同迁移机制。

## RQ3：LatticeKB/EdgeKappaKB 的实际限制是什么？

1. Lattice 已经有 repo-free 公共节点，冷启动不等于完全无先验。它依赖语法特征子集；无法从未记录的输入规模、测试数量、依赖构建状态推导资源尺度。完全未见核心 executable/target 时可能 abstain。
2. 精确匹配直接取经验样本，不保证跨仓库或小样本可靠。variance shrinkage 作用于选择风险，而非直接缩减预测均值或校准 P90。真实失败例：`20c/ctl` 的 `python -` 实测 426 ms，选中 `tool=python, flag:-` 的 6 条源样本后 P50 为 28414 ms；`3yourmind/django-migration-linter` 的短 inline assertion 实测 332 ms，回退到 `tool=python` 的 197 条源样本后预测 25996 ms。语法命令族基线显式区分 Python module/inline/script，比仅按 executable 更具体；本批次收益不需要用 LLM 才能解释。LOSO、最大特征数、去 repo 与 own-node accumulation 的实际结果全部保存在 `e3_kb_ablation.csv`；不能把覆盖率变化单独叫作收益。
3. **Edge 的已学习边权不会直接迁移到未见过的完整节点。** `edge_kappa_kb/kb.py::_predict` 明确在 `exact=False` 时使用 `k0 / len(parents)`，只在已有 child 上读取存储的 log-kappa。repository-disjoint 查询携带新的 repo 特征，通常形成全新 child。实测 1078 个零样本 duration 查询中，exact=0，learned 与 fixed 的 P50/P90 不同数=0（含双方 unavailable 的一致结果）。因此“源数据训练了权重”不等于“新仓库继承了这些权重”。父节点的真实分布仍可迁移。
4. Edge 原算法仅 duration；局部反馈修正哪些边取决于已查询/已覆盖的节点和保存的事前 token。`edge_fixed` 和 `edge_freeze_target` 区分源训练与目标反馈。Lattice 去 repo 还会改变 bounded graph 的节点预算、完整签名分组和精确匹配，不能把全部变化解释成一个单纯的仓库过滤开关。
5. `failure_cases.json` 保存最差的真实命令、目标仓库、实测值、P50/P90、选中上下文、可用源历史与原始定位。上下文和误差是直接证据；输入规模或状态的遗漏只是待验证解释，不伪装为已确定原因。

## RQ4：是否支持开发 LLM-assisted Semantic Knowledge Transfer？

目前不足以给出 GO。数据验证了冷启动评估的现实缺口，也提供了语法迁移与少样本修正的实测基线，但没有隔离“语义知识”相对已有 KB 的增量价值。可优先检验的语义信息是：测试目标数量/规模、测试与收集模式、编译/依赖安装阶段、是否重用缓存、参数的真正作用范围。这些是下一阶段候选，不是本研究证实有效的特征。

下一步最值得验证的问题：在同一原生硬件、已知 CPU quota 与受控并发下，同一测试命令族的输入规模解释，是否能在严格 repository holdout 上改善环境 extra-memory 的 P90 校准，并降低少样本负迁移？需要更多独立仓库、多任务、可核验变体与重复执行。先补足真实测量，再决定是否需要 LLM；无需先做完整仓库扫描器或新机器学习模型。

## 复现与限制

用 `../reproduce.py --flat <原始644份trace目录> --output <全新输出目录>`，同一 ClawTune checkout 中需保留清单引用的本地 `.runtime` 下载。入口输出源清单、代码哈希、配置、日志、fold、逐条预测、汇总和图。图见 `figures/`。统计模型均使用真实标签，无合成命令、资源峰值或测试结果。

本机 Windows/WSL 没有可用 Docker 命令，不能调用现有 Linux/cgroup/eBPF benchmark runner 补采；待运行命令和前提记录在 `docs/CURRENT_PLAN.md`。未把 dry-run 或单元测试当成新的真实测量。

核心限制：未知并发/历史硬件信息、QEMU 开销、两仓库的环境内存覆盖、任务数不足、观测性变体、短调用采样可用性选择偏差，以及训练源运行顺序采用模拟。总体决策依据这些证据缺口，而不是挑选有利的平均收益。
