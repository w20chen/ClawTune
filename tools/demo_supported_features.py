from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Demonstrate supported scheduler features.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--run-launcher", action="store_true")
    parser.add_argument(
        "--command",
        default=(
            "python3 -c \"from pathlib import Path; import hashlib, math, os, time; "
            "p=Path('data/demo-supported-heavy.bin'); p.parent.mkdir(parents=True, exist_ok=True); "
            "blob=bytearray(os.urandom(16*1024*1024)); "
            "total=sum(math.sqrt(i) for i in range(2000000)); "
            "digest=hashlib.sha256(blob).hexdigest()[:16]; "
            "p.write_bytes(blob); data=p.read_bytes(); time.sleep(0.5); "
            "print('clawtune-launch-heavy-ok', len(data), int(total), digest)\""
        ),
    )
    parser.add_argument("--cpu-set", default=None)
    parser.add_argument("--numa-node", type=int, default=None)
    args = parser.parse_args()

    print("== sidecar health ==")
    print_json(get_json(args.endpoint, "/health/live"))
    print_json(get_json(args.endpoint, "/health/ready"))

    print("\n== v1 decision/completion/runtime sample ==")
    decision_demo(args.endpoint)

    print("\n== v2 execution registration ==")
    execution_demo(
        args.endpoint,
        command=args.command,
        run_launcher=args.run_launcher,
        cpu_set=args.cpu_set,
        numa_node=args.numa_node,
    )


def decision_demo(endpoint: str) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tool_call_id = f"demo-call-{uuid4().hex[:8]}"
    request = {
        "schema_version": "clawtune.v1",
        "event_id": f"evt-before-{uuid4().hex[:8]}",
        "occurred_at": now,
        "plugin_version": "demo",
        "run_id": "demo-run",
        "session_id": None,
        "session_key": None,
        "agent_id": "demo-agent",
        "tool_call_id": tool_call_id,
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": "pytest",
        "derived_paths": [],
        "params_digest": "sha256:" + "0" * 64,
        "param_features": {
            "serialized_size_bytes": 42,
            "string_length": 16,
            "list_item_count": 0,
            "path_count": 0,
            "has_command_like_field": True,
        },
        "raw_params": None,
        "resource_scope": None,
    }
    decision = post_json(endpoint, "/v1/decisions/tool", request)
    print("decision:")
    print_json(decision)

    completion = {
        "schema_version": "clawtune.v1",
        "event_id": f"evt-after-{uuid4().hex[:8]}",
        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "plugin_version": "demo",
        "run_id": "demo-run",
        "session_id": None,
        "session_key": None,
        "agent_id": "demo-agent",
        "tool_call_id": tool_call_id,
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "exec",
        "duration_ms": 25,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": 0,
        "resource_scope": None,
    }
    print("completion:")
    print_json(post_json(endpoint, "/v1/events/tool-completed", completion))
    recent = get_json(endpoint, "/v1/tools/recent?limit=1")
    print("latest runtime sample:")
    print_json(recent)


def execution_demo(
    endpoint: str,
    *,
    command: str,
    run_launcher: bool,
    cpu_set: str | None,
    numa_node: int | None,
) -> None:
    execution_id = f"demo-exec-{uuid4().hex[:8]}"
    placement: dict[str, object] = {}
    if cpu_set:
        placement["cpu_set"] = cpu_set
    if numa_node is not None:
        placement["numa_node"] = numa_node
    registration = post_json(
        endpoint,
        "/v2/executions",
        {
            "execution_id": execution_id,
            "tool_call_id": f"demo-call-{uuid4().hex[:8]}",
            "run_id": "demo-run",
            "session_key_hash": None,
            "command_digest": "sha256:" + "1" * 64,
            "command": command,
            "workdir": str(ROOT),
            "host": "gateway",
            "placement": placement or None,
            "profiling": {
                "mode": "proc",
                "enable_cgroup": True,
                "enable_affinity": True,
                "enable_numa": True,
            },
            "backend": "managed-wrapper",
        },
    )
    print("registration:")
    print_json(registration)

    if not run_launcher:
        print("launcher not run. To claim this spec manually:")
        print(
            "  python -m clawtune_sidecar.launcher run "
            f"--endpoint {endpoint} --execution-id={execution_id} "
            f"--token={registration['one_time_token']}"
        )
        return

    env = os.environ.copy()
    scheduler_src = str(ROOT / "services" / "scheduler" / "src")
    env["PYTHONPATH"] = scheduler_src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "clawtune_sidecar.launcher",
            "run",
            "--endpoint",
            endpoint,
            f"--execution-id={execution_id}",
            f"--token={registration['one_time_token']}",
        ],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    print(f"launcher exit code: {result.returncode}")
    print("execution scope:")
    print_json(get_json(endpoint, f"/v2/executions/{urllib.parse.quote(execution_id)}/scope"))


def get_json(endpoint: str, path: str) -> object:
    with urllib.request.urlopen(endpoint.rstrip("/") + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(endpoint: str, path: str, payload: object) -> object:
    request = urllib.request.Request(
        endpoint.rstrip("/") + path,
        method="POST",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} failed with HTTP {exc.code}: {detail}") from exc


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
