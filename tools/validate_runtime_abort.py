#!/usr/bin/env python3
"""Linux acceptance: completed tool followed by a killed launcher, at concurrency 4."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
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
sys.path[:0] = [str(ROOT), str(ROOT / "services/sidecar/src")]

import uvicorn
from benchmarks.exec_control import SidecarExecutions
from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from swe_rebench.host_openclaw import _abort_runtime, _drain_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Cached Linux image with python3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    runtimes = ["abort-check-" + uuid.uuid4().hex[:12] for _ in range(args.concurrency)]
    state = build_state(SidecarConfig(
        trace_dir=args.output / "sidecar", tool_resource_artifact_dir=args.output / "kb",
        trace_runtime_paths={json.dumps(["swe-rebench", runtime]): str(args.output / runtime / "trace.jsonl") for runtime in runtimes},
        tool_resource_ebpf_required=True, execution_cgroup_root="/sys/fs/cgroup/clawtune",
        pmu_max_active=args.concurrency, max_global_concurrency=args.concurrency,
        docker_exec_observer_enabled=False, llm_proxy_enabled=False,
    ))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(state), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("sidecar startup timeout")
        time.sleep(.02)
    client = SidecarExecutions(port)

    def execute(index):
        reason = "cancelled" if index % 2 else "task_timeout"
        runtime = runtimes[index]
        folder = args.output / runtime
        folder.mkdir()
        container = subprocess.check_output([
            "docker", "run", "--pull=never", "-d", "--network=host",
            "--name", runtime, "-v", f"{ROOT}:/clawtune:ro",
            args.image, "sleep", "600",
        ], text=True).strip()
        process = None
        def launch(eid, command, log):
            token = client.register(execution_id=eid, runtime_id=runtime,
                                    tool_call_id=eid, command=command, repo="abort-acceptance")
            env = dict(os.environ, CLAWTUNE_EXECUTION_TOKEN=token)
            return subprocess.Popen([
                "docker", "exec", "-e", "CLAWTUNE_EXECUTION_TOKEN",
                "-e", "PYTHONPATH=/clawtune/services/sidecar/src",
                "-e", "CLAWTUNE_LAUNCH_MODE=fork-exec", "-e", "CLAWTUNE_CGROUP_REQUIRED=1",
                container, "python3", "-m", "clawtune_sidecar.launcher", "run",
                "--execution-id", eid, "--endpoint", client.base,
            ], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            done = runtime + "-completed"
            with (folder / "completed.log").open("w") as log:
                process = launch(done, "python3 -c 'print(sum(i*i for i in range(2000000)))'", log)
                assert process.wait(timeout=90) == 0, (folder / "completed.log").read_text()
            _drain_runtime(port, runtime, gateway_id="swe-rebench", timeout_seconds=30, flush_kb=False)
            completed = state.predictor.execution_telemetry(done)
            assert completed and completed.status == "ok", completed
            assert completed.kb_observations_added > 0, completed
            artifact = Path(completed.artifact_path)
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            profile = state.pmu_collector.take(done).to_dict()
            assert profile["coverage"]["eligible_for_kb"], profile
            orphan = runtime + "-interrupted"
            with (folder / "interrupted.log").open("w") as log:
                process = launch(orphan, "python3 -u -c 'import time; print(\"READY\"); time.sleep(300)'", log)
                deadline = time.monotonic() + 90
                while "READY" not in (folder / "interrupted.log").read_text():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError((folder / "interrupted.log").read_text())
                    time.sleep(.1)
                record = state.executions.get(orphan)
                assert record.claimed and not record.exited and state.predictor.execution_active(orphan)
                waiting = client._request("POST", f"/v1/gateways/swe-rebench/runtimes/{runtime}/drain?timeout_seconds=0", {})
                assert waiting["drained"] is False, waiting
                # Remove the sandbox while the launcher waits: no /exited can be sent.
                subprocess.run(["docker", "rm", "-f", container], check=True, capture_output=True, timeout=30)
                process.wait(timeout=10)
            assert not record.exited, "test did not reproduce lost /exited"
            _abort_runtime(port, runtime, gateway_id="swe-rebench", reason=reason, trace_dir=folder)
            _drain_runtime(port, runtime, gateway_id="swe-rebench", timeout_seconds=30, flush_kb=False)
            response = next(json.loads(line)["payload"] for line in (folder / "trace.jsonl").read_text().splitlines() if json.loads(line).get("event_type") == "runtime_finalization")
            assert response["aborted_execution_ids"] == [orphan], response
            assert not response["pmu_profiles"][orphan]["coverage"]["eligible_for_kb"]
            incomplete = state.predictor.execution_telemetry(orphan)
            assert incomplete.unavailable_reason == reason, incomplete
            assert incomplete.artifact_summary["replay_execution"] == "incomplete", incomplete
            assert incomplete.kb_observations_added == 0, incomplete
            assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
            assert not state.predictor.active_execution_ids(runtime, "swe-rebench")
            assert client._request("POST", f"/v1/gateways/swe-rebench/runtimes/{runtime}/abort",
                {"reason": reason, "agent_stopped": True, "sandbox_cleaned": True}) == response
            traces = list(folder.glob("*.jsonl"))
            assert traces == [folder / "trace.jsonl"], traces
            rows = [json.loads(line) for line in traces[0].read_text().splitlines()]
            events = [row for row in rows if row.get("event_type") == "execution_telemetry"]
            assert {row["execution_id"] for row in events} == {done, orphan}, rows
            assert len(events) == 2 and all(isinstance(row.get("artifact"), dict) for row in events)
            assert sum(row["record_type"] == "trace_metadata" for row in rows) == 1
            return {"runtime_id": runtime, "completed": done, "aborted": orphan, "reason": reason,
                    "retained_observations": completed.kb_observations_added,
                    "incomplete_artifact": incomplete.artifact_path}
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    results = []
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(execute, range(args.concurrency)))
        _drain_runtime(port, results[-1]["runtime_id"], gateway_id="swe-rebench", timeout_seconds=60)
        assert not state.executions.active()
        assert state.pmu_collector.diagnostics()["active"] == 0
        report = {"status": "ok", "concurrency": args.concurrency, "results": results, "kb_flushed": True}
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        server.should_exit = True
        thread.join(timeout=60)
        sock.close()


if __name__ == "__main__":
    main()
