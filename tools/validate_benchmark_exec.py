#!/usr/bin/env python3
"""Linux Docker acceptance for Terminal and the shared benchmark launcher.

Uses cached images only, creates task-owned containers, and exercises real
HTTP lifecycle endpoints, procfs identity resolution, and PMU counting. No
model credentials or benchmark reference datasets are used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/sidecar/src"))

import uvicorn
from benchmarks.backends import TerminalBackend
from benchmarks.exec_control import SidecarExecutions
from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Cached image with /bin/sh and python3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--require-ebpf", action="store_true")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    state = build_state(SidecarConfig(
        trace_dir=args.output / "sidecar",
        tool_resource_artifact_dir=args.output / "artifacts",
        tool_resource_ebpf_required=args.require_ebpf,
        pmu_max_active=args.concurrency,
        max_global_concurrency=args.concurrency,
        docker_exec_observer_enabled=True,
        docker_exec_container_prefix="",
        llm_proxy_enabled=False,
    ))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(state), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("sidecar did not start")
    client = SidecarExecutions(port)
    containers = []
    profiles = []
    failures = []
    command = "python3 -c 'print(sum(i*i for i in range(500000)))'"

    def execute(index: int, mode: str, container: str):
        runtime_id = f"{mode}-{index}"
        call_id = "call-1"
        backend = TerminalBackend.__new__(TerminalBackend)
        backend.container = container
        backend.timeout = 120
        backend.deadline = time.monotonic() + 180
        backend.sidecar = client
        backend.runtime_id = runtime_id
        backend.gateway_id = "swe-rebench"
        backend.repo = "pmu-acceptance"
        backend.log_dir = args.output / runtime_id
        backend.log_dir.mkdir()
        backend.gate_available = False
        backend.telemetry_required = True
        backend.gate_install_error = None
        backend._register_container_scope()
        common = dict(
            schema_version="clawtune.v1", occurred_at=datetime.now(timezone.utc).isoformat(),
            plugin_version="0.1.0", gateway_id="swe-rebench", runtime_id=runtime_id,
            run_id="acceptance", session_id=runtime_id, session_key=None, agent_id=None,
            tool_call_id=call_id, tool_name="terminal_exec" if mode == "terminal" else "exec",
        )
        before = dict(common, event_id=f"{runtime_id}-before", tool_kind="shell",
                      tool_input_kind="json", operation_hint=None, derived_paths=[],
                      params_digest="sha256:" + "a" * 64,
                      param_features=dict(serialized_size_bytes=len(command), string_length=len(command),
                                          list_item_count=0, path_count=0, has_command_like_field=True),
                      raw_params={"command": command}, resource_scope=None)
        decision = client._request("POST", "/v1/decisions/tool", before)
        if mode == "subprocess":
            # Earlier waves must feed real evidence into later predictions.
            targets = decision["prediction"]["pmu_prediction"]["targets"]
            for metric in ("ipc", "llc_mpki", "llc_miss_rate"):
                assert targets[metric]["evidence_count"] > 0, targets
                for quantile in ("avg", "p50", "p90"):
                    assert isinstance(targets[metric][quantile], (int, float)), targets
        started = time.monotonic()
        if mode == "terminal":
            backend._install_exec_gate()
            assert backend.gate_available, backend.gate_install_error
            result = backend.call("terminal_exec", {"command": command}, call_id=call_id)
        else:
            execution_id = "acceptance-" + uuid.uuid4().hex
            token = client.register(execution_id=execution_id, runtime_id=runtime_id,
                                    tool_call_id=call_id, command=command, repo=backend.repo)
            env = os.environ.copy()
            # Docker obtains the token from the environment, not its argv.
            env["CLAWTUNE_EXECUTION_TOKEN"] = token
            run = subprocess.run([
                "docker", "exec", "-e", "CLAWTUNE_EXECUTION_TOKEN",
                "-e", "PYTHONPATH=/clawtune/services/sidecar/src",
                "-e", f"CLAWTUNE_LAUNCH_MODE={mode}",
                "-e", "CLAWTUNE_LAUNCH_DEBUG=1", container,
                "python3", "-m", "clawtune_sidecar.launcher", "run",
                "--execution-id", execution_id, "--endpoint", client.base,
            ], env=env, capture_output=True, text=True, timeout=180)
            result = dict(exit_code=run.returncode, stdout=run.stdout, stderr=run.stderr)
        assert result["exit_code"] == 0 and "41666541666750000" in result["stdout"], result
        records = state.executions.for_runtime(runtime_id, "swe-rebench")
        assert len(records) == 1 and records[0].exited, "execution did not finalize"
        record = records[0]
        assert record.trusted_root_pid is not None
        profile = state.pmu_collector.take(record.request.execution_id)
        assert profile is not None
        data = profile.to_dict()
        profiles.append(dict(runtime_id=runtime_id, mode=mode, profile=data))
        assert data["coverage"]["status"] == "reliable", data
        assert data["coverage"]["eligible_for_kb"], data
        for event in ("cycles", "instructions", "llc_read_accesses", "llc_read_misses"):
            assert data["events"][event]["raw_count"] is not None, data
        for event in ("cycles", "instructions"):
            assert data["events"][event]["raw_count"] > 0, data
        completion = dict(common, event_id=f"{runtime_id}-after",
                          execution_id=None if mode == "terminal" else record.request.execution_id,
                          decision_id=decision["decision_id"], lease_id=decision["lease_id"],
                          duration_ms=int((time.monotonic()-started)*1000), succeeded=True,
                          error_type=None, error_digest=None, result_size_bytes=len(result["stdout"]),
                          raw_result=result, resource_scope=None)
        client._request("POST", "/v1/events/tool-completed", completion, timeout=60)
        if mode == "terminal" and index == 0:
            backend.timeout = 1
            timeout_command = "sleep 30 & echo $! > /tmp/clawtune-timeout-child; wait"
            try:
                backend.call("terminal_exec", {"command": timeout_command}, call_id="timeout-call")
                raise AssertionError("timeout command unexpectedly completed")
            except subprocess.TimeoutExpired:
                pass
            status = subprocess.check_output([
                "docker", "exec", container, "sh", "-c",
                "pid=$(cat /tmp/clawtune-timeout-child); cat /proc/$pid/stat 2>/dev/null || true",
            ], text=True, timeout=5).strip()
            assert not status or status.rsplit(")", 1)[1].split()[0] == "Z", "timeout descendant still running"
            records = state.executions.for_runtime(runtime_id, "swe-rebench")
            timed = next(r for r in records if r.request.tool_call_id == "timeout-call")
            assert timed.exited and timed.signal, "timeout lifecycle was not finalized"
            assert state.pmu_collector.take(timed.request.execution_id).coverage.eligible_for_kb is False
        return runtime_id

    try:
        for index in range(args.concurrency):
            container = subprocess.check_output([
                "docker", "run", "--pull=never", "-d", "--network=host",
                "--name", "clawtune-pmu-acceptance-" + uuid.uuid4().hex[:12],
                "-v", f"{ROOT}:/clawtune:ro", args.image, "sleep", "600",
            ], text=True).strip()
            containers.append(container)
            # Reproduce task images whose login profile launches extra programs.
            subprocess.run([
                "docker", "exec", container, "sh", "-c",
                "printf '\\nid >/dev/null\\n' >> /etc/profile",
            ], check=True, timeout=10)
        for mode in ("terminal", "fork-exec", "subprocess"):
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = [pool.submit(execute, i, mode, c) for i, c in enumerate(containers)]
                for future in futures:
                    try:
                        future.result()
                    except Exception as exc:
                        failures.append(f"{mode}: {type(exc).__name__}: {exc}")
        state.trace_writer.flush()
        # Check final sidecar traces, not only the in-memory collector result.
        traced = set()
        for path in (args.output / "sidecar").glob("*.jsonl"):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                execution = row.get("execution") or {}
                if execution.get("execution_id"):
                    traced.add(execution["execution_id"])
                    if args.require_ebpf and row.get("record_type") == "span_end":
                        telemetry = (execution.get("tool_resource") or {}).get("call_telemetry") or {}
                        if not telemetry.get("eligible_for_kb") or telemetry.get("telemetry_quality") != "ok":
                            failures.append(f"invalid clause telemetry: {execution['execution_id']}")
        expected = {item["profile"]["execution_id"] for item in profiles}
        if not expected <= traced:
            failures.append(f"missing final execution traces: {expected - traced}")
    finally:
        for container in containers:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()
        (args.output / "report.json").write_text(json.dumps({
            "status": "failed" if failures else "ok", "require_ebpf": args.require_ebpf,
            "profiles": profiles, "failures": failures,
        }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"profiles": len(profiles), "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
