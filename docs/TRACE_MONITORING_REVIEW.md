> Superseded implementation status: the final candidate and Linux acceptance
> instructions are in [CURRENT_PLAN.md](CURRENT_PLAN.md). The current policy is
> eBPF preferred with explicit dedicated-cgroup fallback, not eBPF-only. Earlier
> completion claims below describe the first implementation, before follow-up
> fixes for clocks, coverage, counters and lease ownership.

# Trace 监测审查与下一步修改计划

审查日期：2026-09-16。基线：main 最后一次提交 `5e68953031daf4013888558de182986e7dc9c917`（`update`）。随后已按本计划完成工作区实现；未修改 OpenClaw core 或 trace 数据集。

结论：该提交修复了部分错误窗口和训练标签污染，但没有解决采集链路与指标语义的问题。不能通过继续放宽可用性规则来修复。下一步应建立默认 eBPF 的统一 tool-call 观测链路，再统一 trace 与学习端的字段语义和质量判断。

证据范围：提交 diff、当前调用链、现有单元测试及内存中构造的复现。仓库内发现的 JSONL 是测试 fixtures，没有本次真实部署的 trace，因此本文不声称测得真实日志的字段缺失率。Linux 内核采集精度尚未实测。

## 需要保留的修复

- 排除 completion 处理阶段的环境内存采样，避免把收尾开销当成执行峰值。
- 拒绝宿主服务 cgroup 作为已验证任务环境；显式表达部分内存不可用原因。
- 独立内存采样线程，避免 process/network 初始化阻塞其轮询。
- 未知/零时长不再作为零延迟训练样本，独立 PMU 指标仍分别判断。
- benchmark 的 gateway 身份和 runtime assets 按运行隔离。

## 发现及优先级

### P1：默认 CPU/memory 路径不是 eBPF，非 exec 更没有统一接入

位置：`monitoring/process.py:64-115,174-217`；`monitoring/tool_runtime.py:75-98`；`predictors/tool_resource.py:915-993`；插件 `src/index.ts:1715-1740`。

`RealtimeToolMonitor` 默认使用 `ProcessResourceSampler`：PID 读 psutil，cgroup 读 `cpu.stat` / `memory.current`。eBPF 则经 `begin_execution` / command SDK 生成 execution/clause telemetry，没有作为顶层 `resources` 的统一来源。最后一次提交没有改变这点。

非 exec 默认绑定 OpenClaw runtime PID 或自身 cgroup；web 工具强制 runtime PID。这样得到的是共享运行时资源，既不是默认 eBPF，也不能证明单次调用独占这些资源。即使 execution 内 eBPF 数据正常，顶层字段依然可能缺失或与其不同。

### P1：CPU 平均值的 1ms 端点条件导致正常长工具失去标签

位置：`monitoring/tool_runtime.py:217-236`；`predictors/tool_resource.py:1729-1733,1871-1878`。

开始快照在 sidecar 的 before 处理中取得，结束快照在 completion 接收/收尾之后取得；实际代码还先执行 `finalize_ebpf_from_completion`。两者不应被要求恰好与 action 两端相差不超过 1ms。进程退出后又会使用最后一次存活采样，正常尾部间隔也超过 1ms。

已复现：10 秒 action、CPU 累计差 1 秒、结束采样只迟到 5ms，`cpu_utilization_avg_cores=None`，继而 `cpu_time_eligible=False`。这是提交新增规则带来的系统性拒绝，不限于短工具。

同时 `cpu_time_s` 原始差值仍导出，disk/network 速率仍除以 action 时长，RSS peak 仍可能包含 completion 后的快照。只收紧 CPU average 没有解决其它字段的窗口错配。

### P1：晚绑定资源 scope 不会启动/更新环境内存监测

位置：`monitoring/tool_runtime.py:95-98,303-400`；`monitoring/environment_memory.py:122-146`。

内存 `begin` 只在 tool before 调用一次。exec 的初始 scope 可以为空；后续 `bind_scope` 更新 process sampler，却不通知 environment memory。已复现：`bind_scope=True`，内存 active 数仍为 0。开始时若绑定了错误/共享环境，后续权威 PID/cgroup 升级也不会同步切换内存对象。

此外，无 scope、无路径和读失败的一些分支静默返回，completion 得到 `None`，而不是 `memory_eligible=false` 加原因。现有不可用协议并未覆盖完整生命周期。简单在晚绑定处补一次 begin 也不够：执行已经开始时不能伪造事前 baseline。

### P1：memory.current、RSS、环境增量存在语义混用

位置：`monitoring/process.py:174-203`；`trace.py:398-402`；`telemetry/cgroup_resource.py:115-132`；`contracts/environment-memory.schema.json`。

`_snapshot_cgroup` 直接令 `rss_bytes=memory_current`，继而写入 `rss_peak_bytes` / `memory_rss_*`。memory.current 是 cgroup 及其后代的内存计费总量，不是进程 RSS。psutil 的进程 RSS 和 eBPF distinct-mm RSS 也不能冒充包含缓存/内核计费的环境总内存。

`extra=max(0, peak-baseline)` 可以定义为所测环境的增量，但不是某个工具的净分配量或独占内存需求。即使排除了已知并发 tool call，环境后台活动仍可能影响它。迁移到 eBPF 时必须区分测量口径，不能将 RSS 填入原有环境计费字段继续训练。

Linux 语义依据：[cgroup v2 文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)、[proc/RSS 文档](https://docs.kernel.org/filesystems/proc.html)。后者也说明 RSS 与 PSS 的区别；distinct-mm 去重只能避免线程重复计算同一地址空间，不能消除不同进程共享物理页的重复计数。

### P1：共享 PID 的网络计数重置互相干扰，来源记录也不真实

位置：`monitoring/process.py:33-45,147-172`；`monitoring/net_accounting.py:202-233`；`telemetry/cgroup_resource.py:100-105,138-143`。

`_net_accounting_isolated` 对所有 `kind=pid` 返回 True，没有排除 `shared-runtime-process`。因此 web_search/web_fetch 等调用会 reset 同一 runtime TGID 的 BPF 计数器。已复现共享 runtime scope 仍触发 reset。并发调用会互相覆盖基线，非并发时也仍包含运行时后台流量。

读数只枚举当时存在的 PID；已退出子进程的累计网络数据可能遗漏。eBPF 不可用时会透明回退到网络 namespace 的 `/proc/<pid>/net/dev`，却没有逐指标来源/归因标记；`cgroup_resource.network_source` 即使对 eBPF 结果也标为 procfs。

### P1：现有 eBPF RSS 的“可用”不代表时间覆盖充分

位置：`tool_resource/telemetry.py:949-972,2182-2233`；`tool_resource/clause_bridge.py:294`。

perf 回调读取当前正在运行任务的 RSS，采样不是对所有活跃地址空间的均匀墙钟快照。长时间阻塞/睡眠但持有内存的进程需要单独考虑。

`_sampled_peak_rss` 记录最大间隙，却只要求两个 RSS 样本就返回 `ok`。已复现：10 秒窗口、只有起止两点、0 个 perf 样本、`max_intersample_gap_frac=1.0`，仍返回可用峰值。bridge 的 `_merge_rss` 也只要求至少两个样本，并在首尾观测之间持有旧值，没有采样陈旧度上限。这是长工具也会遇到的覆盖问题，不是要求优化超短工具。

虽然 CPU 和内存都可以用 eBPF，二者不能机械共用“当前在 CPU 上运行的任务”这一采样总体。此处需要有限度修改 `services/sidecar/src/tool_resource`；仅在 sidecar 外围接线无法修复已有 RSS reducer 的语义。

### P2：存活进程累计值的差分会漏计已退出进程

位置：`monitoring/process.py:346-408`；`monitoring/tool_runtime.py:472-508,699-711`。

每次重新枚举存活子进程并求 CPU/IO/context-switch 累计和，进程退出后总和可能下降。负数截成 0 会隐藏漏计，而不是恢复累计值。简单复现累计序列 `5,1,2` 时相对起点的 CPU 差全部为 0。也不应跨 PID 复用、exec epoch 或 counter reset 直接相减。

### P2：trace 的时钟、覆盖率和来源没有同一套可信定义

位置：`trace.py:248-254,1168-1217`；`monitoring/tool_runtime.py:674-693`。

span 的 monotonic end 在写 trace 时生成，start 由 duration 倒推，数值并非 action 真实的 monotonic 起止。`_snapshot_from_point` 还将 wall timestamp 赋给 monotonic 字段，scope 重建后可能混合时钟域。

coverage 只计算首尾区间重叠，不能证明中间有采样或某个指标可用。两个相距很远的快照可以得到完整区间覆盖；不能把此值当成全部资源的质量。trace scope 根据有无 cgroup_path 生成，但 sampler 可能已因 trusted root PID 改成 process-tree，元数据也可能与实际来源不一致。

## 下一步实施顺序

### 1. 先定义公共协议和指标口径

- 以 JSON Schema 为源定义 tool resource observation；TS/Pydantic/validator/fixtures 同步，并明确旧 trace 读取兼容和新版本迁移。
- 逐指标记录 `value/unit/measurement/source/scope/attribution/window/availability/reason`；区分“观测可用”和“可作为独占调用训练标签”。具体字段命名在实现时统一，避免只给全体指标一个 `quality`。
- CPU 分开定义累计 CPU 时间、实际观测窗口平均核数、完整 action 平均核数、固定 500ms 窗口峰值。没有完整 action 证据时，不把部分窗口平均值伪装成 action 平均值。
- 默认内存指标采用明示的 eBPF sampled distinct-mm RSS；标注近似内核计数和采样峰值，不承诺真实瞬时峰值/PSS/环境计费总量。采样 baseline、窗口峰值、增量必须同源同 scope。
- 旧 `memory_total_peak_bytes` / `memory_extra_peak_bytes` 若继续表示环境计费量，则在默认 eBPF 路径明确不可用；不能塞入 RSS。新增 RSS 口径相应预测目标/索引，旧模型按 measurement 隔离，禁止静默混训。

### 2. 接入常驻 eBPF tool-call 观测服务

- collector 按 runtime/host 生命周期初始化，工具调用注册观测窗口；不要让 shell command parsing、execution_id、exec 事件成为 CPU/memory 采集的前置条件。
- 支持 attach 已存在的 runtime PID/TID，并用 host 身份、PID namespace、starttime 和 fork/exec/exit 建立稳定集合。exec 跟踪可信 root 与后代；在退出时保留最终累计量，保留 mm 生命周期身份，避免 PID/mm 地址复用造成混账。
- 工具资源默认 eBPF；不因为缺样本、不支持探针或非 exec 而自动使用 cgroup/psutil 替代。权限、内核能力、事件丢失和 scope 不明都输出具体不可用原因。
- cgroup 可继续作为隔离、成员定位、quota 上下文；这与使用 cgroup counters 作为默认测量来源不同。只有显式启用兼容/诊断模式时才读取替代计数，并在 trace 标记来源，禁止同字段无标记混用。
- 复用现有 eBPF 生命周期/计数能力与 clause 归因；tool-call 聚合不依赖 clause parser 成功。不要直接求和不同 clause 的 RSS 峰值或 CPU 峰值。

### 3. 为非 exec 保留诚实、可用的共享运行时观测

- 所有工具走相同 eBPF 后端，非 exec 注册 runtime PID 的窗口，而不是默认 runtime cgroup 计数。
- 已能证明独占 worker/进程的工具可以输出 per-tool 指标。共享 Node 进程中的异步工具只能先输出 `shared_runtime` 窗口观测，记录并发与背景污染可能性，不能作为独占成本标签。
- 没有并发工具也不能自动证明无后台工作。eBPF 能识别内核任务，不能凭 PID 分辨同一事件循环里多个 JS 请求的 CPU/堆归属。不修改 OpenClaw core 的前提下，若插件 API 没有可信异步关联证据，本阶段不承诺独占归因。
- 本地 web 工具只描述本地 runtime 开销，不代表远端 API 服务的 CPU/memory。
- 共享计数不 reset；使用每个窗口自己的快照或不可变事件查询，避免窗口之间互相影响。

### 4. 统一生命周期、窗口和 reducer

- 记录 producer 实际 action 边界、collector 实际观察边界、接收/写入时间，保留 clock domain 与转换依据；同一内核使用 monotonic 时间轴关联，不拿不同主机/VM 的 monotonic 值直接比较。
- scope 晚绑定、重绑定和结束必须同时通知所有指标 collector。预先存在的观测可以用于窗口查询；没有事前样本就明确 baseline 不可用，不能倒填。
- 依据真实采样时间、边界证据、丢失事件和可接受采样间隙判断质量，删除跨 HTTP 快照精确对齐到 1ms 的门槛。不能简单将 1ms 放宽后继续把错窗累计量除以 action 时长。
- CPU 使用稳定任务身份的累计增量或调度事件，防止成员退出造成倒退；只在有证据的窗口聚合，不将计数回退当成真实 0。
- 内存采样需覆盖活跃 mm，包括不在 CPU 上运行但仍存活的任务。先在目标内核验证定时 BPF task 遍历/等价周期采集能力；选择经过能力探测的内核方案，无法覆盖时明确缺失，不能改读 cgroup 掩盖。采样中保留每个 mm 的时间、年龄和生命周期；限制过期值延续与跨长间隙推断。
- 保持常规采样节奏，不为低于采样间隔的工具增加忙等、延长执行或特殊高频路径。短工具/不足 500ms 窗口可返回 `insufficient_samples` / `insufficient_window`；CPU 总量有独立有效证据时仍可单独使用。

### 5. 统一 trace、在线学习和回放

- 顶层资源、execution telemetry 和 clause telemetry 使用同一组有 provenance 的原始观测与明确聚合器；保留各自窗口和粒度，不暗中替换。
- 修复 RSS 命名、network backend 标记、fallback 标记、scope 标记和时钟字段；所有缺失指标必须可解释，不再依赖静默省略猜原因。
- 将部分窗口原始数据放在诊断视图，主字段与训练目标只接收符合各自语义的数据。在线与离线共用 eligibility 逻辑；失去一个指标不拖累独立有效指标。
- 旧 trace 中语义模糊的 RSS/cgroup 字段只按旧 measurement 读取，不自动升级为新 eBPF 标签。迁移重建产物写入新目录，不修改研究/评估 trace。
- 主要改动范围：plugin scope/事件协议、sidecar monitoring/API/trace、contracts、predictor 输入适配。`tool_resource` 仅作通用 eBPF 接入和必要 reducer/measurement 适配；避免无关 KB 重构。Placement 保持 advisory。

## 实施结果

- 新增 `tool_resource_observation_v1` 公共 JSON Schema；trace、在线学习、离线回放和 TypeScript/Pydantic 协议均使用同一组逐指标 eligibility/reason 字段。
- `RealtimeToolMonitor` 无显式诊断 sampler 时默认使用共享 eBPF source。cgroup/procfs 只用于身份解析；权限、事件丢失、晚绑定、共享 scope 和采样间隙都返回不可训练原因，不自动 fallback。
- 非 exec 工具走同一 eBPF 窗口；共享 runtime/sandbox 只保留诊断观测，禁止作为独占训练标签。共享 PID 的网络计数不再 reset。
- CPU action 窗口使用 producer Linux monotonic 边界和真实采样间隙，CPU 峰值按固定 500ms 窗口积分；采样 RSS 使用 distinct-mm、20ms bin 和 150ms 最大间隙限制。
- `memory.current` 不再写入 RSS 字段；新增 `sampled_peak_rss_bytes` 预测目标。旧环境 memory measurement 与新 RSS measurement 不混训。
- 已持久化晚绑定、稀疏采样、共享 scope、schema 校验和默认 eBPF 路径测试；插件完成事件携带 action monotonic 边界和 clock domain。

## 验收与验证记录

实现应新增针对真实失效模式的回归：晚绑定、正常长调用的 completion 延迟、已退出后代、共享 PID 重叠窗口、无权限/丢事件、错误时钟域、RSS 长间隙/睡眠持有内存、同 mm 线程去重、PID/mm 复用、旧 trace 回放，以及默认路径不读取 cgroup counters。不要把提高短工具可用率作为目标。

Linux 集成验收至少覆盖：数秒 CPU 工作负载、多线程/派生后代、分配并触碰内存后睡眠、并发非 exec、混合 exec/非 exec、后台流量、故意延迟 completion。用独立已知负载/校验来源检查 CPU 数量级和采样 RSS 口径；cgroup 数据可作旁路对照但不要求与 RSS 数值相等。默认模式应证明没有自动 fallback，长工具有正确来源和质量说明，短工具可以诚实不可用。真实 trace 统计应按工具、时长、scope、backend 和 unavailable reason 分组，避免只追求总体非空率。

本次在 Windows 运行（仓库根目录，先设 `$env:PYTHONPATH='services/sidecar/src'`）：

```powershell
python -m pytest services/sidecar/tests/test_memory_window.py services/sidecar/tests/test_tool_runtime_monitor.py services/sidecar/tests/test_single_task_trace.py services/sidecar/tests/test_cgroup_resource.py -q -rs
# 相关回归通过；完整 sidecar 套件见下方
python -m pytest services/sidecar/tests/test_resource_contract_v2.py services/sidecar/tests/test_tool_resource_telemetry.py tests/test_benchmark_runtime_fixes.py -q -rs
# 相关回归通过
```

完整可运行的 sidecar 测试为 `521 passed, 4 skipped`；其中包含新增 eBPF reducer/default-path 回归。插件 `npm.cmd test` 为 `104 passed`。测试只证明协议、窗口和 eligibility 逻辑，不证明 Linux 实测准确性。

Linux eBPF / 真实 provider benchmark 待验收命令和环境限制见 [CURRENT_PLAN.md](CURRENT_PLAN.md)。
