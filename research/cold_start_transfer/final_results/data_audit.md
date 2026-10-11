# 数据审计

读取 814 份真实 trace；导出 15506 条 clause target-view 记录。内存专用记录不重复计入 duration。

| Stratum | Files | Labeled repos | Labeled tasks | Duration | Extra memory | RSS |
|---|---|---|---|---|---|---|
| v5:fresh-seed42-skip150-n200 | 275 | 0 | 0 | 0 | 0 | 0 |
| v5:offline-gated-confirm-100-v1 | 93 | 0 | 0 | 0 | 0 | 0 |
| v5:pennylane-all76-c1-fast-ebpf-20260813 | 70 | 1 | 70 | 5036 | 0 | 2464 |
| v5:source-image-direct-six-v1 | 6 | 0 | 0 | 0 | 0 | 0 |
| v5:sqlglot-100-c2-fast-requested-ebpf-a0419d9-20260803 | 52 | 1 | 52 | 1969 | 0 | 1167 |
| v5:sqlglot-48-c2-fast-requested-ebpf-8a72722-20260803 | 48 | 1 | 48 | 1841 | 0 | 1074 |
| v5:sqlglot-prev100-c2-fast-requested-ebpf-20260804 | 100 | 1 | 100 | 4395 | 0 | 2601 |
| v6:batch-20260913:swe | 30 | 11 | 30 | 778 | 0 | 778 |
| v6:batch-20260913:terminal | 19 | 0 | 19 | 722 | 0 | 722 |
| v6:benchmark-validation:bfcl-run | 13 | 0 | 0 | 0 | 0 | 0 |
| v6:benchmark-validation:research-direct-run | 5 | 0 | 0 | 0 | 0 | 0 |
| v6:benchmark-validation:verified-run | 3 | 1 | 1 | 3 | 0 | 3 |
| v6:experiment-30x2:swe-run-v2 | 18 | 3 | 3 | 5 | 0 | 5 |
| v6:experiment-30x2:terminal-run | 20 | 0 | 3 | 3 | 0 | 0 |
| v6:experiment-30x2:terminal-supplement-run | 14 | 0 | 3 | 7 | 0 | 0 |
| v6:terminal-20260921 | 4 | 0 | 4 | 186 | 0 | 166 |
| v6:tool-profile-study:terminal-run-v4 | 25 | 0 | 3 | 4 | 0 | 4 |
| v6:trace-audit:20260920-054917-d62adf68 | 6 | 2 | 6 | 166 | 0 | 146 |
| v6:trace-audit:20260920-105843-4abef2c5 | 4 | 2 | 4 | 140 | 0 | 133 |
| v6:trace-audit:20260920-134712-3bd11c46 | 2 | 2 | 2 | 52 | 0 | 45 |
| v6:trace-audit:20260920-153840-eaa0dfcc | 2 | 2 | 2 | 45 | 0 | 42 |
| v6:trace-audit:20260920-172943-8ea96fce | 2 | 2 | 2 | 40 | 11 | 36 |
| v6:trace-audits:20260921-041901-94a4b095 | 3 | 2 | 3 | 76 | 27 | 71 |

## 标签与缺失

在导出的观测视图中：

| Target | Valid | Missing in exported views | Missing fraction |
|---|---|---|---|
| duration_ms | 15468 | 38 | 0.002451 |
| sampled_peak_rss_bytes | 9457 | 6049 | 0.3901 |
| memory_extra_peak_bytes | 38 | 15468 | 0.9975 |
| cpu_time_seconds | 705 | 14801 | 0.9545 |

这是已通过至少一种目标门槛的 clause 视图分母，不是全部工具调用分母；不能据此估算整体采样成功率。所有文件的工具调用数量、无 clause 观测、拒收原因分别保存在 `../final_data/data_quality.json`。其中 v5 `no_clause_observation` 共 21574 次，`withheld_clause_cpu_without_interval_evidence` 共 16476 次。原始 CPU 累积值不等于已验证 exec 区间增量。

原始执行审计覆盖 32371 条工具记录，其中报告 failure 421 条、标记 censored 37 条。正常完成的非零退出与被截断执行不同：前者可以有有效 duration，后者不能当完整执行标签。逐文件失败比例在 `source_execution_quality.csv`；每种 clause 指标的原始 availability/missing reason 计数在 `raw_availability_reasons.csv`，包括完整的无标签调用分母，不仅是进入模型的样本。

较新的环境内存工具级有效观测计数为 102；它们没有被复制成 clause 标签。独立合格的 clause extra-memory 为 38 条，来自 2 个仓库。没有把 RSS 当作准入内存，也没有把缺失值补零。
原始 `memory_eligible` 标记不是充分条件：复核测量 cgroup 与执行 cgroup 的实际路径后，隔离了 23 条工具级和 19 条 clause 级旧内存标签。20260920-134712 批次测量 Docker cgroup，但执行已迁至其外部的 `/sys/fs/cgroup/clawtune/exec-*`，因此这些 `cgroup_v2_memory_current` 标签不代表执行负载，零值也不能当作真实零需求。拒收依据是范围不匹配而非数值大小；路径、原值、源哈希和行号保存在 `../final_data/memory_scope_quarantine.json`。合格 clause 内存来自后续 `cgroup_v2_environment_union_v1`，两个 run 仍分开；没有把不同 measurement 混合。

v5 多仓库批次的大量命令缺少 clause telemetry。它们的 hook duration 不能替代当前模型的 retained-workload/clause duration。具有完整 clause 标签的历史批次主要是 pennylane 与 sqlglot，批次、并发和主机证明不足，分别保留，不跨批次拼成一个貌似受控的多仓库实验。历史 RSS 输入按既有 seed recipe 的 MiB 解释；未取得历史 producer 版本的独立单位证明，因此仅作单独诊断。

## 来源与环境

完整源路径、SHA-256、benchmark、repo、task、run、命令、原始时间戳、hardware、measurement source 在 `../final_data/source_manifest.json` 和 `observations.jsonl`。未记录的 base commit、CPU quota、并发和架构保留为空，不猜测。相同仓库不同 commit 始终同一 holdout 身份。

主分析为 2026-09-13 SWE 实测批次：11 个仓库、30 个任务、778 条保留 clause。原始 artifact 中 `quota_cores=320` 是记录的环境配置值，不表示分配了 320 个独占核或实际消耗了这些核。PMU 记录主机 aarch64，exec provenance 明确 `qemu-x86_64`。该导出没有足够的并发证明，保留 unknown；因此这里只是批次内观察性比较，不是排除了同机干扰的因果试验。

后续批次按采集 run、主机架构、quota、QEMU/native 证据、并发和 memory measurement 分层。相同两仓库的重复批次不是新的独立仓库。Terminal、BFCL、Deep Research 没有可验证 repository 身份时仅纳入数据审计，不把 task/category 当作 repository。SWE-bench Verified 按其真实 repo 单独分层。

已有源文件哈希复核 814 个，变化 0；重复 execution identity 0。所有来源拒收和本地未下载文件均保留在质量审计 JSON；fixture、合成测试和旧预测输出不作为标签。
