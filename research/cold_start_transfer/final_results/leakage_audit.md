# 泄漏审计

已验证 68 个 repository/target/condition fold，仓库交集、校准与测试任务交集均为 0。结果：`leakage_checks.json`。

- 每个 fold 从空模型拟合，初始训练不包含目标仓库的任何任务、重试、commit 或标签。仓库标识转为小写，跨 benchmark 前缀不构成不同仓库。
- 不加载任何公共 seed、旧 KB 或旧预测。bundled seed 含 10 个 sqlglot case，不能无条件用于 sqlglot 留出实验；记录在 `seed_contamination_audit.json`。
- n 指当前目标有效的 **clause 执行观测数**，不是工具调用数或任务数。只开放前 n 条。为防止同任务状态/重试泄漏，整个前缀触及的任务均从测试排除；所有 n 使用该 fold 最大支持 n 下相同的测试执行。只有一个任务的仓库仅支持 n=0。
- 按首条有效观测时间排列目标任务，任务内按记录时间/ID 排序。为了建立严格 repository holdout，把全部源仓库训练置于目标之前，使用公开、确定性的模拟串行时钟：start=2i、end=2i+1。这不声称恢复真实在线跨任务次序，也不保持并发重叠。所有资源数值仍为原始实测值，未由模拟时钟重算。每次提交严格满足 end < query start。
- Edge 源训练复用原 `fit` 的 seeded task replay；本地反馈使用 `begin`/`complete`/`commit_before`。测试结果从不在线回灌；只允许已声明的 calibration prefix。
- 校准采用 source-only family P50 的事前残差，中位数后乘 n_family/(n_family+lambda)。lambda 候选 0、2、5，在源仓库内部 leave-one-repository-out 验证，按仓库等权平均选择。duration 选择 log MAE；资源选择单位缩放后的 p90 pinball。无可用内部少样本验证时，n>0 的 candidate 返回 unavailable（诊断中保留未使用的 default=5）；n=0 仍等于未校准先验。范围固定为相同语法 family，不能传播到无关工具。
- 原始 KB 参数不调优；各消融全部报告。E1 候选规则不根据测试收益挑选。未使用测试标签选择模型、筛选有利仓库或生成合成参数对。

`folds/*/split.json` 保存逐条训练、前缀、测试 ID 与内部验证仓库；预测文件保存逐条 availability、真实值、预测分位数、证据数量、选中上下文和 Edge parent 诊断。所有方法共用目标标签集合，`e3_kb_ablation.csv` 使用与 Lattice 都可预测的成对样本；不能将各自覆盖子集上的平均误差直接解释为改进。Bootstrap 以仓库为单位，少于 5 个仓库仅描述，不给置信区间。
