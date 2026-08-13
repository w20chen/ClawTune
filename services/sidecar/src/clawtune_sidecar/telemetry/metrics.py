from __future__ import annotations

from collections import Counter

from clawtune_sidecar.monitoring.tool_runtime import ToolRuntimeSample


class Metrics:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.decision_latencies: list[float] = []
        self.tool_durations: list[float] = []
        self.admission_waits: list[float] = []
        self.tool_cpu_seconds_total = 0.0
        self.tool_io_read_bytes_total = 0
        self.tool_io_write_bytes_total = 0
        self.tool_net_rx_bytes_total = 0
        self.tool_net_tx_bytes_total = 0
        self.tool_context_switches_total = 0
        self.latest_tool_memory_rss_bytes = 0
        self.latest_tool_memory_rss_peak_bytes = 0
        self.latest_tool_process_count = 0
        self.latest_tool_cpu_utilization_avg_cores = 0.0
        self.latest_tool_io_read_bytes_per_second = 0.0
        self.latest_tool_io_write_bytes_per_second = 0.0
        self.latest_tool_net_rx_bytes_per_second = 0.0
        self.latest_tool_net_tx_bytes_per_second = 0.0

    def inc(self, name: str) -> None:
        self.counters[name] += 1

    def observe_tool_runtime(self, sample: ToolRuntimeSample) -> None:
        self.counters["scheduler_tool_runtime_samples_total"] += 1
        status = sample.attribution_status.replace("-", "_")
        self.counters[f"scheduler_tool_runtime_{status}_samples_total"] += 1
        if sample.cpu_time_delta_s is not None:
            self.tool_cpu_seconds_total += sample.cpu_time_delta_s
        if sample.read_bytes_delta is not None:
            self.tool_io_read_bytes_total += sample.read_bytes_delta
        if sample.write_bytes_delta is not None:
            self.tool_io_write_bytes_total += sample.write_bytes_delta
        if sample.net_rx_bytes_delta is not None:
            self.tool_net_rx_bytes_total += sample.net_rx_bytes_delta
        if sample.net_tx_bytes_delta is not None:
            self.tool_net_tx_bytes_total += sample.net_tx_bytes_delta
        if sample.ctx_switches_delta is not None:
            self.tool_context_switches_total += sample.ctx_switches_delta
        if sample.rss_bytes_after is not None:
            self.latest_tool_memory_rss_bytes = sample.rss_bytes_after
        if sample.rss_bytes_peak is not None:
            self.latest_tool_memory_rss_peak_bytes = sample.rss_bytes_peak
        if sample.process_count_after is not None:
            self.latest_tool_process_count = sample.process_count_after
        if sample.cpu_utilization_avg_cores is not None:
            self.latest_tool_cpu_utilization_avg_cores = sample.cpu_utilization_avg_cores
        if sample.disk_read_bytes_per_s is not None:
            self.latest_tool_io_read_bytes_per_second = sample.disk_read_bytes_per_s
        if sample.disk_write_bytes_per_s is not None:
            self.latest_tool_io_write_bytes_per_second = sample.disk_write_bytes_per_s
        if sample.net_rx_bytes_per_s is not None:
            self.latest_tool_net_rx_bytes_per_second = sample.net_rx_bytes_per_s
        if sample.net_tx_bytes_per_s is not None:
            self.latest_tool_net_tx_bytes_per_second = sample.net_tx_bytes_per_s

    def render(
        self,
        active_leases: int,
        active_tool_monitors: int = 0,
        active_lease_mcpu: int = 0,
    ) -> str:
        lines = []
        names = [
            "scheduler_tool_requests_total",
            "scheduler_tool_decisions_total",
            "scheduler_tool_completions_total",
            "scheduler_tool_runtime_samples_total",
            "scheduler_tool_runtime_pid_samples_total",
            "scheduler_tool_runtime_unattributed_samples_total",
            "scheduler_tool_runtime_pid_unavailable_samples_total",
            "scheduler_sidecar_errors_total",
            "scheduler_calibration_updates_total",
        ]
        for name in names:
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {self.counters[name]}")
        lines.append("# TYPE scheduler_active_leases gauge")
        lines.append(f"scheduler_active_leases {active_leases}")
        lines.append("# TYPE scheduler_active_lease_millicores gauge")
        lines.append(f"scheduler_active_lease_millicores {active_lease_mcpu}")
        lines.append("# TYPE scheduler_active_tool_monitors gauge")
        lines.append(f"scheduler_active_tool_monitors {active_tool_monitors}")
        lines.append("# TYPE scheduler_tool_cpu_seconds_total counter")
        lines.append(f"scheduler_tool_cpu_seconds_total {self.tool_cpu_seconds_total:.6f}")
        lines.append("# TYPE scheduler_tool_memory_rss_bytes gauge")
        lines.append(f"scheduler_tool_memory_rss_bytes {self.latest_tool_memory_rss_bytes}")
        lines.append("# TYPE scheduler_tool_memory_rss_peak_bytes gauge")
        lines.append(f"scheduler_tool_memory_rss_peak_bytes {self.latest_tool_memory_rss_peak_bytes}")
        lines.append("# TYPE scheduler_tool_process_count gauge")
        lines.append(f"scheduler_tool_process_count {self.latest_tool_process_count}")
        lines.append("# TYPE scheduler_tool_cpu_utilization_avg_cores gauge")
        lines.append(f"scheduler_tool_cpu_utilization_avg_cores {self.latest_tool_cpu_utilization_avg_cores:.6f}")
        lines.append("# TYPE scheduler_tool_io_read_bytes_total counter")
        lines.append(f"scheduler_tool_io_read_bytes_total {self.tool_io_read_bytes_total}")
        lines.append("# TYPE scheduler_tool_io_write_bytes_total counter")
        lines.append(f"scheduler_tool_io_write_bytes_total {self.tool_io_write_bytes_total}")
        lines.append("# TYPE scheduler_tool_io_read_bytes_per_second gauge")
        lines.append(f"scheduler_tool_io_read_bytes_per_second {self.latest_tool_io_read_bytes_per_second:.6f}")
        lines.append("# TYPE scheduler_tool_io_write_bytes_per_second gauge")
        lines.append(f"scheduler_tool_io_write_bytes_per_second {self.latest_tool_io_write_bytes_per_second:.6f}")
        lines.append("# TYPE scheduler_tool_net_rx_bytes_total counter")
        lines.append(f"scheduler_tool_net_rx_bytes_total {self.tool_net_rx_bytes_total}")
        lines.append("# TYPE scheduler_tool_net_tx_bytes_total counter")
        lines.append(f"scheduler_tool_net_tx_bytes_total {self.tool_net_tx_bytes_total}")
        lines.append("# TYPE scheduler_tool_net_rx_bytes_per_second gauge")
        lines.append(f"scheduler_tool_net_rx_bytes_per_second {self.latest_tool_net_rx_bytes_per_second:.6f}")
        lines.append("# TYPE scheduler_tool_net_tx_bytes_per_second gauge")
        lines.append(f"scheduler_tool_net_tx_bytes_per_second {self.latest_tool_net_tx_bytes_per_second:.6f}")
        lines.append("# TYPE scheduler_tool_context_switches_total counter")
        lines.append(f"scheduler_tool_context_switches_total {self.tool_context_switches_total}")
        lines.append("# TYPE scheduler_decision_latency_seconds summary")
        lines.append(f"scheduler_decision_latency_seconds_count {len(self.decision_latencies)}")
        lines.append(f"scheduler_decision_latency_seconds_sum {sum(self.decision_latencies):.6f}")
        lines.append("# TYPE scheduler_tool_duration_seconds summary")
        lines.append(f"scheduler_tool_duration_seconds_count {len(self.tool_durations)}")
        lines.append(f"scheduler_tool_duration_seconds_sum {sum(self.tool_durations):.6f}")
        lines.append("# TYPE scheduler_admission_wait_seconds summary")
        lines.append(f"scheduler_admission_wait_seconds_count {len(self.admission_waits)}")
        lines.append(f"scheduler_admission_wait_seconds_sum {sum(self.admission_waits):.6f}")
        return "\n".join(lines) + "\n"
