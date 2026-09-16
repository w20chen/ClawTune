# 工具画像字段参考

工具画像由调用开始时的预测、调用结束时的实测、关联的执行与子命令遥测组成。公开结构以 [JSON Schema](../contracts/) 为准；下面说明如何读取数值和判断是否可用。不同采集器独立工作，不能用一个总覆盖率代替所有指标的有效性。

`null` 或字段缺失表示没有该测量；`0` 仅表示当前测量范围内计数为零。`available`、`eligible_for_kb` 和 `memory_eligible` 均不代表预测准确度或资源上限。超时和取消是不完整执行，不能作为完整执行的训练标签。历史 trace 不会因为升级采集器而自动变准确；验证新采集时使用新的 run-local KB，避免旧标签继续影响预测。

## 采样频率与测量范围

完成事件的 `occurred_at` 必须是带时区的有效时间戳；无效输入返回 422，不会用当前时间替代。`duration_ms=0` 可能表示缺失或不足毫秒精度，不作为零耗时训练样本；独立有效的 PMU 标签不受此限制。

| 数据源 | 频率或触发方式 | 解释 |
| --- | --- | --- |
| 调用级 cgroup / 进程树 | 默认轮询等待 50 ms，开始和结束另取快照 | 20 Hz 是目标；轮询工作耗时会拉长实际间隔。以时间线相邻 `ts` 的差为准。 |
| 环境内存 `memory.current` | 独立线程，默认轮询等待 50 ms | 不与进程树枚举、网络 BCC 初始化共用采样线程；仍受系统调度和文件读取耗时影响。 |
| 子命令 eBPF CPU / RSS | perf callback 每约 10 ms CPU 时间；另有 exec / exit 边界事件 | 不是墙钟 100 Hz；休眠、I/O 等待和多核执行会改变墙钟采样密度。 |
| 子命令磁盘计数 | perf 样本与进程边界的 Linux task I/O 累计计数 | 字节数不是读写系统调用次数，也不包含全部缓存命中读流量。 |
| PMU | 执行启用计数器，结束时读取 | 连续硬件计数，不是 50 ms 抽样。 |
| 网络 | TCP 内核事件计数；不可用时可能退回 namespace 累计计数 | 不代表所有协议的完整流量；子命令网络字段目前没有实现。 |
| 调用和子命令时延 | 生命周期事件差值 | 不要求周期采样。 |

调用级 CPU 峰值使用完整的 500 ms 固定窗口，要求至少三个有效样本、单一来源、累计 CPU 单调、相邻样本间隔不超过 150 ms，且时间线未截断。尾部不足 500 ms 的部分不形成一个完整窗口。子命令 CPU 峰值对短于 1 秒的执行不可用；累计 CPU 时间和时延仍可用。短调用不会一律被丢弃，只有不具备足够证据的目标不可用。

内存标签要求：已确认的任务容器或独占执行 cgroup、无同时监控的重叠环境调用、调用开始前不超过 150 ms 的 baseline、至少一个调用内样本、包括起止边界的采样空隙不超过 150 ms。结束后的样本不参与峰值。总量含后台进程和缓存，增量表示环境占用相对 baseline 的增长，不保证全由当前工具造成。

## Trace 关联与执行结果

| 字段 | 含义 |
| --- | --- |
| `schema_version`, `record_type` | 协议版本和记录种类：`trace_metadata`、`span_start`、`span_end`、`trace_event`。 |
| `trace_format_version`, `created_at`, `mode`, `scaffold` | metadata 中的格式版本、创建时间、运行模式和 agent 框架。 |
| `gateway_id`, `runtime_id`, `run_id`, `session_id`, `agent_id`, `repo` | 网关、运行实例、运行、会话、agent、项目身份。关联时保留 owner 信息，不跨运行仅凭工具名合并。 |
| `trace_id`, `span_id`, `parent_span_id`, `sequence_no` | trace 身份、调用身份、父调用和序号；同一调用的 start / end 共用 span 身份。 |
| `kind`, `name` | `tool` / `llm` 及工具名 / 模型名。本文画像表对应 `kind=tool`。 |
| `wall_time_ns`, `monotonic_time_ns`, `duration_ns` | 墙钟 Unix 纳秒、单调时钟纳秒、耗时纳秒，通常为十进制字符串。sidecar 工具 span 的单调起止是按持续时间重建的，不是原始两次 hook 时钟读数。 |
| `input.requested_args` | 原始请求参数，可能已脱敏。 |
| `status.code`, `status.message` | 调用结果状态和说明。 |
| `output.exit_code`, `output.result` | 解析出的退出码和工具原始结果；原始结果结构随工具不同，退出码缺失不能推断成功。 |
| `execution.mode`, `execution.execution_id` | launcher / runtime-managed 模式和执行身份。 |
| `execution.payload_pid`, `execution.payload_pid_start_time_ticks`, `execution.pid_role` | 被测进程 PID、Linux 进程启动 tick 身份和 PID 的角色。 |
| `execution.cgroup_path`, `execution.source` | 执行 cgroup 和作用域发现来源；不保证所有指标都使用该 cgroup。 |
| `execution.tool_resource` | eBPF 子命令遥测摘要，见下文。 |

## 调用级实测：`span_end.resources`

| 字段 | 单位与含义 |
| --- | --- |
| `action_duration_ns`, `tool_body_ns` | ns；OpenClaw 报告的 action 耗时，后者无原始耗时时可为空。包含框架执行边界内的开销，不等于子命令耗时。 |
| `plugin_window_ns` | ns；插件 before / after hook 之间的单调时钟区间。 |
| `decision_duration_ns` | ns；before hook 决策 / instrumentation 耗时。 |
| `completion_duration_ns` | ns；after hook 完成上报的往返耗时；sidecar 写 trace 时往返尚未结束，可能为空。 |
| `sidecar_overhead_ns` | ns；当前生产端已知的 sidecar 开销。sidecar trace 通常只含 before hook 部分，不能当总开销。 |
| `monitor_start_wall_time_ns`, `monitor_end_wall_time_ns` | Unix ns；采样窗口实际起止。 |
| `monitor_start_monotonic_ns`, `monitor_end_monotonic_ns`, `monitor_duration_ns` | ns；采样窗口单调时钟及持续时间，与 action 区间不同。 |
| `coverage_duration_ns`, `coverage_ratio` | ns、0–1；进程 / cgroup 采样窗口与 action 的交集时长及其占 action 的比例。不是采样密度，也不是独立内存或 PMU 的覆盖率。 |
| `coverage_reason` | 窗口原因，如 `full_window`、`pid_registered_late`、`monitor_window_no_overlap`、`shared_runtime_process`、`shared_sandbox_container`、`pid_unavailable`、`clock_data_missing`。 |
| `attribution_status`, `attribution_source`, `scope` | 归属程度、作用域来源、`cgroup` / `process_tree` / `none`；`attributed` 不保证完整采样。 |
| `quality`, `sampling_quality` | 综合窗口质量和采样器质量。`sampling_quality=ok` 不保证实际达到 50 ms；判断峰值还须检查间隔。 |
| `monitor_source`, `target_pid` | 实际资源采样器和目标 PID。 |
| `sampling_interval_ms`, `sampling_point_count` | ms、次数；配置的轮询间隔和采样数，不是实际平均间隔。 |
| `cpu_time_s`, `cgroup_cpu_time_s` | core-s；采样起止的累计 CPU 差。后者仅在 cgroup 来源时提供，不能与前者相加。进程树采样可能漏掉已退出子进程。 |
| `cpu_utilization_avg_cores` | cores；仅当监控起止与 action 起止分别相差不超过 1 ms（生产端时钟精度）时，取 `cpu_time_s / action_seconds`；否则为空，累计 CPU 诊断值也不作为完整调用的 CPU 训练标签。 |
| `cpu_utilization_avg_pct` | %；`cpu_utilization_avg_cores * 100`，多核可超过 100%。 |
| `cpu_peak_cores`, `cpu_peak_window_ms` | cores、ms；固定窗口平均 CPU 的最大值及窗口长度 500，不是瞬时峰值。 |
| `rss_peak_bytes`, `memory_rss_bytes_before`, `memory_rss_bytes_after` | bytes；历史命名的诊断值：进程来源为进程树 RSS；cgroup 来源实际为 `memory.current`，含缓存，不能统一解释成 RSS。 |
| `memory_baseline_bytes` | bytes；最近的合格调用前环境内存样本。 |
| `memory_total_peak_bytes` | bytes；调用内环境内存样本最大值，不是分配上限。 |
| `memory_extra_peak_bytes` | bytes；`max(0, total_peak - baseline)`。 |
| `memory_environment_id`, `memory_measurement` | 环境身份与测量方法：`cgroup_v2_memory_current`；协议另支持 `guest_memtotal_minus_memavailable`，不是 host VM RSS。 |
| `memory_eligible`, `memory_unavailable_reason` | 是否具备内存标签证据及不可用原因，独立于进程窗口 coverage。 |
| `memory_timeline` | `[Unix秒, bytes]` 样本；合格输出包含 baseline 和调用内样本。 |
| `memory_clause_observations` | 从同一时间线为不重叠子命令生成的训练观察，不是额外内存消耗，不能重复相加。 |
| `disk_read_bytes_delta`, `disk_write_bytes_delta` | bytes；采样区间的存储 I/O 累计差。 |
| `disk_read_bytes_per_s`, `disk_write_bytes_per_s` | bytes/s；上述差值除以 action 秒数。 |
| `net_rx_bytes_delta`, `net_tx_bytes_delta` | bytes；接收 / 发送计数差。存在按进程 TCP 与 namespace fallback 的范围差异，零值不能证明所有协议无流量。 |
| `net_rx_bytes_per_s`, `net_tx_bytes_per_s` | bytes/s；上述差值除以 action 秒数。 |
| `ctx_switches_delta` | 次；采样区间上下文切换累计差，可能漏掉已退出进程。 |
| `process_count_before`, `process_count_after` | 个；起止时可见进程数，不是期间创建的总数。 |
| `resource_class` | 基于预测时延的类别，不是实测 CPU / 内存分类。 |
| `resource_timeline`, `resource_timeline_truncated` | 资源样本列表及是否超过保存上限（默认 2000 点）；截断会使峰值标签不可用。 |
| `cgroup_resource`, `pmu` | 独立资源摘要与硬件计数器画像，详见下文。 |

内存不可用原因：`unverified_task_environment`（宿主服务等未验证环境）、`execution_window_unavailable`（缺少窗口）、`overlapping_environment_calls`（调用重叠）、`baseline_after_execution_start`（baseline 太晚）、`stale_memory_baseline`（baseline 太旧）、`no_in_execution_memory_sample`（无调用内样本）、`memory_sampling_gap`（采样断档）、`memory_timeline_truncated`（工具运行时间超过内存 timeline 保留范围，无法安全还原完整窗口）。缺少 cgroup 或读取失败时也可能没有内存字段。短调用没有样本时按不可用处理。

`memory_clause_observations[]` 含 `repo`, `bin`, `argv`, `ts_start`, `ts_end`（项目、子命令和 Unix秒边界），`in_loop`, `in_pipe`, `in_subst`, `pipeline_position`（命令结构），以及同名的 `memory_baseline_bytes`, `memory_total_peak_bytes`, `memory_extra_peak_bytes`, `memory_measurement`, `memory_environment_id`, `memory_eligible`。为复用观察结构，还保留 `latency_ms`, `cpu_peak_cores`, `sampled_peak_rss_mb`, `cpu_ns_cumulative`；这条仅补充内存的观察中它们为空，不会重复训练 CPU 和时延。

`resource_timeline[]` 每点含 `ts`（Unix秒）、`elapsed_ms`（自该段第一点的毫秒数）、`available`、`source`、`rss_bytes`、`process_count`。累计差字段为 `cpu_time_delta_s`、`read_bytes_delta`、`write_bytes_delta`、`net_rx_bytes_delta`、`net_tx_bytes_delta`、`ctx_switches_delta`，相对同来源段首点。`read_bytes_per_s`、`write_bytes_per_s`、`net_rx_bytes_per_s`、`net_tx_bytes_per_s` 使用相邻样本间隔作分母，与调用级速率分母不同。段首速率没有定义。

`cgroup_resource` 是同一运行时采样结果的另一种字段表示，不是一份可叠加的测量：

| 字段 | 含义 |
| --- | --- |
| `schema`, `execution_id`, `tool_call_id`, `tool_name` | `cgroup_resource_v1` 和关联身份。 |
| `source`, `monitor_source`, `attribution_source` | 实際是 cgroup 还是进程树，以及来源。对象名称不保证使用 cgroup。 |
| `ts_start`, `ts_end`, `duration_ms` | action 的 Unix秒起止与毫秒耗时。 |
| `cpu_time_s`, `cpu_utilization_avg_cores` | 与上表同名字段相同。 |
| `memory_rss_before_bytes`, `memory_rss_after_bytes`, `memory_rss_peak_bytes` | 与上表 RSS 诊断字段对应，同样需按来源解读。 |
| `disk_read_bytes_delta`, `disk_write_bytes_delta` | 存储读写 bytes 差。 |
| `network_rx_bytes_delta`, `network_tx_bytes_delta` | 对应顶层 `net_rx_bytes_delta`、`net_tx_bytes_delta`。 |
| `sampling_interval_ms`, `sampling_point_count`, `sampling_quality`, `sampling_coverage_ms` | 目标间隔、点数、质量和监控时长；最后一个不是 action 交集时长。 |
| `cpu_source`, `memory_source`, `disk_source`, `network_source` | 各目标的来源说明；网络来源标签目前不能细分 BCC 与 namespace fallback。 |
| `fallback_used`, `cgroup_setup_error`, `cgroup_read_error`, `collector_errors`, `independence` | fallback 标记、错误与采集关系说明。错误列表为空不证明没有漏采；`independence` 表示与子命令采集独立。 |

## 子命令画像与采集质量

入口为 `execution.tool_resource.call_telemetry.clauses[]` 或 `trace_event.artifact.calls[].clauses[]`，见 [clause telemetry schema](../contracts/clause-telemetry.schema.json)。两处可能表示同一执行，不能重复计数。

| 字段 | 单位与含义 |
| --- | --- |
| `bin`, `argv` | 子命令可执行文件和参数。 |
| `ts_start`, `ts_end`, `latency_ms` | Unix秒起止、毫秒时延；由执行边界记录，不要求周期采样。 |
| `cpu_ns_cumulative`, `cumulative_cpu_s` | 累计 CPU ns 及 `/1e9` 后的 core-s；后者在摘要中提供。 |
| `peak_cpu_cores` | cores；子命令固定窗口 CPU 峰值，短于 1 秒或质量不足时为空。 |
| `sampled_peak_rss_mb`, `peak_memory_mb` | 十进制 MB（1 MB=1,000,000 bytes）；前者是原始 artifact 的 RSS 诊断字段，后者是摘要中的对应字段，不是环境内存预测目标。 |
| `disk_read_bytes`, `disk_write_bytes` | 摘要中的存储 I/O bytes。 |
| `disk_io.read_bytes_total`, `disk_io.write_bytes_total`, `disk_io.read_write_bytes_total` | 原始 artifact 的累计读、写及两者之和，bytes。 |
| `disk_io.cancelled_write_bytes_total`, `disk_io.availability` | 取消写入的计数与可用性；取消写计数单独保留，不重复相加。 |
| `network_rx_bytes`, `network_tx_bytes` | 预留的子命令网络 bytes，当前不可用。 |
| `status.state`, `status.exit_code`, `status.signal`, `status.succeeded` | 执行状态、退出码、终止信号和是否成功；状态可为 exited、signaled、not_executed、exec_failed、unavailable。 |
| `status.reason`, `status.source` | 状态缺失或终止原因，以及状态证据来源。 |
| `availability.latency`, `availability.cpu`, `availability.memory`, `availability.disk_io`, `availability.status` | 各目标可用性和原因；CPU 峰值不可用不表示累计 CPU 也不可用。 |
| `in_loop`, `in_pipe`, `in_subst`, `pipeline_position` | 是否在循环、管道、替换表达式中及管道位置。 |
| `eligible_for_kb`, `telemetry_quality` | 能否用于学习、采集质量；管道下游查看器等还有独立语义筛选。 |
| `mapping_evidence`, `owned_exec_image_count`, `provenance` | 静态命令与运行进程映射证据、拥有的 exec image 数和底层采集诊断。 |

`execution.tool_resource` 摘要字段：`started`（采集是否开始）、`status` / `unavailable_reason`（遥测状态）、`execution_id` / `tool_call_id`（关联身份）、`artifact_path`（原始路径，下载后可能不可访问）、`artifact_summary`（采集器摘要）、`call_telemetry`（命令、质量、子命令）、`kb_observations_added`（观察写入数）、`kb_update_error`（学习更新错误）。写入数不是全部目标的有效样本数。

原始 artifact 的 `schema`, `version`, `mode`, `status_model` 定义格式；`container_id`, `cgroup_id`, `quota_cores` 定义环境和 CPU 配额。`telemetry_quality`, `collection_validity`, `formal_completeness`, `replay_execution`, `integrity` 分别描述质量、采集有效性、完整性、执行完成状态和一致性错误。`collector` 含状态、健康、禁用原因、有效 / 无效 call 数和 `kprobe_total_hits`；命中数不是工具资源计数。`cleanup` 是清理结果，`provenance` 是采集参数和证据。

`telemetry_loss_total` 的 `ringbuf_reserve_failures`, `argv_read_failures`, `argv_boundary_read_failures`, `total` 为丢失 / 读取失败计数；`ring_loss_total` 是 ring 丢失诊断。`call_coverage` 的 `eligible_call_count`, `withheld_call_count`, `total_call_count`, `eligible_fraction` 为可学习、扣留、总 call 数及比例，不是周期采样覆盖率。

每个 artifact call 另有 `command`, `tool_call_id`, `version`, `tool_trace_ref`（输入和关联）、`telemetry_quality`, `eligible_for_kb`, `target_availability`, `invalid_reasons`, `integrity`, `telemetry_loss`, `ring_loss`（质量）、`mapping`, `candidate_rejections`, `coverage_gaps`, `no_runtime_exec`, `runtime_invocations`, `static_word_intent`, `transition_graph`, `provenance`（命令映射、拒绝候选、覆盖缺口、未执行分支和执行归属证据）。这些开放诊断对象不应作为稳定资源指标使用；尤其不能把未执行分支算成零耗时训练样本。

## PMU：`resources.pmu`

见 [PMU profile schema](../contracts/pmu-profile.schema.json)。四个 `events` 计数与五个 `derived` 指标构成九个硬件画像目标：

| 字段 | 单位 / 公式 |
| --- | --- |
| `events.cycles` | cycles；CPU 周期。 |
| `events.instructions` | instructions；退休指令。 |
| `events.llc_read_accesses` | 次；末级缓存读访问。 |
| `events.llc_read_misses` | 次；末级缓存读未命中。 |
| `derived.ipc` | instructions/cycle；instructions / cycles。 |
| `derived.llc_mpki` | misses / 1000 instructions；1000 × misses / instructions。 |
| `derived.llc_miss_rate` | 0–1；misses / accesses。 |
| `derived.llc_read_accesses_per_cpu_second` | 次 / 计数器运行秒；accesses / 对应事件的 `(time_running_ns/1e9)`。 |
| `derived.llc_read_misses_per_cpu_second` | 次 / 计数器运行秒；misses / 对应事件的 `(time_running_ns/1e9)`。 |

每个 event 含 `supported`, `semantics`, `raw_count`, `scaled_count`, `time_enabled_ns`, `time_running_ns`, `running_ratio`, `error`，分别表示支持性、硬件语义、原始计数、缩放计数、启用时间、运行时间、运行 / 启用比例、错误。零分母对应的比率不可用。这些指标不是 DRAM 带宽。仿真器执行时，计数包含宿主执行的仿真工作，不能解释成原生 guest 指令画像。

PMU 其余字段：`schema`, `execution_id`, `source`, `mode`, `scope`, `root_pid`, `started_at`, `ended_at`, `architecture`, `pmu_devices`, `llc_semantics`, `llc_semantics_confirmed`, `collector_errors`，描述版本、身份、采集来源 / 模式 / 范围、根进程、Unix秒起止、架构、设备、LLC 语义是否确认和错误。`coverage.status`, `reason`, `running_ratio`, `multiplexed`, `kernel_included`, `root_and_future_descendants`, `eligible_for_kb` 描述可靠性、原因、运行比例、复用、是否含内核与后代、能否学习。不能用进程采样 coverage 代替此处判定。

## 预测：`span_start.prediction`

权威负载预测为 [call_prediction](../contracts/call-load.schema.json)，硬件预测为 [pmu_prediction](../contracts/pmu-prediction.schema.json)。`duration_p50_ms`, `duration_p90_ms`, `resource_class`, `confidence` 是顶层兼容摘要；`confidence` 不是经过校准的成功概率。

`call_prediction.targets` 包含全部六个目标：

| 目标 | 单位 | 含义 |
| --- | --- | --- |
| `duration_ms` | ms | 调用耗时。 |
| `cpu_time_seconds` | core_seconds | 归属工作负载累计 CPU。 |
| `cpu_avg_cores` | cores | 同一观察的累计 CPU / 耗时。 |
| `cpu_peak_cores` | cores | 500 ms 固定窗口峰值。 |
| `memory_total_peak_bytes` | bytes | 环境内存采样峰值，含背景。 |
| `memory_extra_peak_bytes` | bytes | 环境峰值相对 baseline 的非负增量。 |

每个目标具有 `status`, `unit`, `metric_definition`, `avg`, `p50`, `p90`, `buckets`, `backend`, `method`, `evidence_counts`, `sample_count`, `context`, `assumptions`, `calibration`, `unavailable_reason`：可用性、单位、测量定义、均值、中位数、经验 p90、直方图、后端、直接 / 合成方法、历史证据数、统计样本数、匹配上下文、假设、校准状态和不可用原因。不可用目标的统计值必须为空。p50 为中位数，p90 为排序后第 `ceil(0.9n)` 个值；不是未来 90% 保证。合成预测的 `sample_count` 可为 2048 次模拟，历史样本数看 `evidence_counts`。

`buckets.edges`, `interval`, `probabilities` 为边界、左闭右开规则、各桶概率；从 0 到首边界、相邻边界之间、最后边界到正无穷各一个桶。概率总和为 1。

`call_prediction.schema_version`, `scope`, `lifecycle`, `cpu_peak_window_ms`, `quantile_method`, `memory_measurement` 声明协议、调用范围、生命周期、峰值窗口、分位数方法、内存来源。`clause_predictions[]` 中每项有 `clause_index`, `argv`, `cwd`, `env_names`, `scope`, `targets`, `memory_measurement`，表示子命令索引、参数、工作目录、环境变量名及相同的六目标预测；子命令耗时和调用耗时不可互换。

`pmu_prediction.targets` 包含上文九个 PMU 目标。每项具有 `status`, `unit`, `metric_definition`, `avg`, `p50`, `p90`, `backend`, `method`, `evidence_count`, `context`, `calibration`, `unavailable_reason`。注意这里是单数 `evidence_count`，没有负载预测的 `sample_count` 或 `buckets`。其 `schema_version`, `scope`, `lifecycle`, `quantile_method` 声明协议、范围、完整执行画像生命周期和分位数方法。

`diagnostics.backends.runtime`, `.trie`, `.lattice` 保留各后端相同结构的六目标预测用于比较，不应当作另外三份实测值。

## 兼容预测与其他诊断

`prediction.tool_resource` 属于 [tool decision schema](../contracts/tool-decision.schema.json) 的兼容诊断，消费者优先使用 `call_prediction`：

| 字段 | 含义 |
| --- | --- |
| `repo`, `command`, `parse_failed`, `clause_bins` | 项目、命令、解析失败标志及子命令列表。 |
| `prediction`, `clause_predictions[]` | 整体 / 分子命令时延桶预测；子项用 `clause_index`, `bin`, `argv`, `prediction`, `unavailable_reason` 关联。 |
| `bucket_id`, `probability_by_bucket`, `scope`, `key_kind`, `evidence_count`, `fallback_path` | 桶编号、桶概率、证据范围、匹配类型、历史证据数和回退路径。 |
| `unavailable_reason` | 预测不可用原因。 |
| `continuous_predictions` | latency_ms、cpu_peak_cores、memory_total_peak_bytes、memory_extra_peak_bytes 的历史 p90。每项目标包含 `target`, `conditional_p90`, `scope`, `key_kind`, `evidence_count`, `fallback_path`, `note`。 |
| `lattice_time_predictions`, `lattice_resource_predictions` | 子命令的 shrinkage、LOSO、max-cardinality 方法结果和资源分布，字段见下表。 |
| `composed`, `composed_total_ms`, `composition` | 是否合成、合成耗时、各串行 / 管道组的 `kind`, `bins`, `time_ms`, `dropped_viewer_bins`。 |
| `prediction_algorithms` | 算法清单及其输入目标、输出和来源说明。 |
| `kv_ttl_cost` | KV 缓存保留策略模拟，不是模型服务的实测缓存指标。 |

两种 lattice 列表的子项均用 `clause_index`, `bin`, `argv` 标识子命令，`predictions[]` 存放各算法结果。资源列表还声明 `scope`, `memory_metric`, `cpu_peak_window_ms`, `quantile_method`：归属范围、环境内存口径、CPU 峰值窗口和分位数方法。

| `predictions[]` 字段 | 含义 |
| --- | --- |
| `algorithm` | `shrinkage`、`loso` 或 `max_cardinality`。 |
| `prediction_ms` | 时延列表的点预测，ms；不可用时为空。 |
| `target`, `unit` | 资源列表的目标及单位：累计 CPU、平均核数、峰值核数、环境内存总峰值或额外峰值。 |
| `p50`, `p90` | 资源列表的经验分位数，不是置信区间。 |
| `selected_features` | 最终用于匹配的特征集合。 |
| `evidence_count` | 匹配到的历史观察数。 |
| `selected_risk` | 算法用于选择候选的风险分数；不是失败概率，也不能跨算法直接比较。 |
| `exact_match` | 是否精确匹配；不适用时为空。 |
| `fallback` | 时延列表的回退说明。 |
| `unavailable_reason` | 缺少有效预测的原因。 |
| `threshold`, `probability_ge` | 资源列表的阈值及历史值大于等于阈值的比例；阈值单位同目标，不是校准后的未来超限概率。 |

`prediction_algorithms.enabled[]` 含 `name`, `family`, `source`, `targets`, `outputs`，分别为算法名、算法族、来源、目标和输出；`excluded[]` 含 `name`, `source`, `reason`，说明未启用的算法及原因。

`kv_ttl_cost` 含 `buckets_s`, `ttl_by_bucket_s`, `initial_bucket_index`, `final_bucket_index`, `num_bucket_jumps`, `bucket_exhausted`, `ttl_s`, `kv_eviction_time_s`, `kv_retention_time_s`, `reference_runtime_s`, `kv_cache_miss`, `miss_penalty_s`, `proxy_cost_s`：桶边界 / TTL（秒）、初末桶、跳桶次数、桶耗尽标志、TTL、驱逐时刻、保留时间、参考时长、模拟未命中、惩罚和代理成本。它不能证明真实缓存命中率。

`placement_advice.cpu_set`, `numa_node`, `llc_cluster`, `advisory` 是建议 CPU / NUMA / LLC 位置及 advisory 标志，MVP 不承诺实际绑核。`decision_id`, `action`, `reason_code`, `reason`, `policy_name`, `policy_version`, `lease_id` 是决策和租约字段，不是资源实测。`placement`, `profiling` 为可选扩展。

## 服务聚合指标

Prometheus 服务指标聚合多个调用，不是单工具画像。`scheduler_` 前缀下的计数器包括 `tool_requests_total`, `tool_decisions_total`, `tool_completions_total`, `tool_runtime_samples_total`, `tool_runtime_pid_samples_total`, `tool_runtime_unattributed_samples_total`, `tool_runtime_pid_unavailable_samples_total`, `sidecar_errors_total`, `calibration_updates_total`，分别统计请求、决策、完成、采样、PID 采样、无归属、PID 不可用、错误和学习更新。

资源累计计数器为 `tool_cpu_seconds_total`, `tool_io_read_bytes_total`, `tool_io_write_bytes_total`, `tool_net_rx_bytes_total`, `tool_net_tx_bytes_total`, `tool_context_switches_total`。当前服务内存中累计，重启会重置；缺测不会补齐，不能等同整台机器的资源总量。

Gauge 包括 `active_leases`, `active_lease_millicores`, `active_tool_monitors`，以及 `tool_memory_rss_bytes`, `tool_memory_rss_peak_bytes`, `tool_process_count`, `tool_cpu_utilization_avg_cores`, `tool_io_read_bytes_per_second`, `tool_io_write_bytes_per_second`, `tool_net_rx_bytes_per_second`, `tool_net_tx_bytes_per_second`。后者是最近一次可用完成样本，缺测可能保留上一值，不是当前所有工具的瞬时总量。

`decision_latency_seconds`, `tool_duration_seconds`, `admission_wait_seconds` 各提供 `_count` 和 `_sum`，分别表示决策、工具时长、准入等待的样本数和秒数总和，不提供 p50 / p90。
