"""Collector setup belongs to service startup, before the first tool decision."""
import asyncio
import threading
from types import SimpleNamespace

from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.contracts.models import ResourceScope
from clawtune_sidecar.monitoring import ebpf_tool, net_accounting
from tool_resource import telemetry


def test_startup_waits_for_monitor_preparation_off_event_loop(monkeypatch, tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path / "traces"))
    entered, release = threading.Event(), threading.Event()
    threads = []

    def prepare():
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(state.tool_monitor.ebpf_monitor, "prepare", prepare)
    app = create_app(state)

    async def run():
        lifespan = app.router.lifespan_context(app)
        task = asyncio.create_task(lifespan.__aenter__())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not task.done()
            assert len(threads) == 1
            assert threads[0] != threading.get_ident()
        finally:
            release.set()
            await task
            await lifespan.__aexit__(None, None, None)

    asyncio.run(run())


def test_monitor_prepares_without_scope_and_reuses_network_for_first_tools(monkeypatch):
    prepared, networks = [], []
    monkeypatch.setattr(telemetry, "prepare_shared_bpf_source", lambda: prepared.append(True))

    class Network:
        available = True

        def __init__(self, inodes):
            self.inodes = set(inodes)
            networks.append(self)

        def add_namespace(self, inode):
            self.inodes.add(inode)

        def rx_tx_for(self, _pids):
            return (0, 0)

        def close(self):
            self.available = False

    monkeypatch.setattr(net_accounting, "ProcessNetAccounting", Network)
    monkeypatch.setattr(net_accounting, "_pid_namespace_inode", lambda pid: pid + 100)
    monkeypatch.setattr(ebpf_tool._KernelWindow, "open", lambda scope: SimpleNamespace(initial_pids={scope.pid}))
    monitor = ebpf_tool.EbpfToolCallMonitor()
    try:
        monitor.prepare()
        monitor.prepare()
        assert prepared
        assert len(networks) == 1
        assert networks[0].inodes == set()
        assert monitor._active == {}
        for pid in (1, 2):
            window = monitor._open_default(ResourceScope(kind="pid", pid=pid))
            assert window.network is networks[0]
        assert networks[0].inodes == {101, 102}
        assert len(networks) == 1
    finally:
        monitor.stop()
    assert networks[0].available is False


def test_preparation_failure_preserves_unavailable_observation(monkeypatch):
    def unavailable(*args):
        raise RuntimeError("ebpf_permission_denied")

    monkeypatch.setattr(telemetry, "prepare_shared_bpf_source", unavailable)
    monkeypatch.setattr(ebpf_tool._KernelWindow, "open", unavailable)
    monitor = ebpf_tool.EbpfToolCallMonitor()
    try:
        monitor.prepare()
        monitor.begin("read", ResourceScope(kind="pid", pid=123))
        result = monitor.complete("read")
        assert result["metrics"]["cpu_time"]["available"] is False
        assert result["metrics"]["cpu_time"]["eligible"] is False
    finally:
        monitor.stop()


def test_network_prepared_with_empty_allowlist_accepts_first_namespace(monkeypatch):
    class Table(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key.value, value.value)

    tables = {name: Table() for name in ("claw_allowed_pid_namespaces", "claw_net_rx", "claw_net_tx")}

    class Bpf:
        def __init__(self, **kwargs):
            pass

        def attach_kretprobe(self, **kwargs):
            pass

        def __getitem__(self, name):
            return tables[name]

        def cleanup(self):
            pass

    monkeypatch.setattr(net_accounting, "_ensure_bcc_importable", lambda: SimpleNamespace(BPF=Bpf))
    network = net_accounting.ProcessNetAccounting(())
    try:
        assert network.available
        assert tables["claw_allowed_pid_namespaces"] == {}
        network.add_namespace(123)
        assert tables["claw_allowed_pid_namespaces"] == {123: 1}
    finally:
        network.close()
