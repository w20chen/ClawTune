from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one heavier raw trace + resource demo run.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    print("== sidecar health ==")
    print_json(get_json(args.endpoint, "/health/live"))
    print_json(get_json(args.endpoint, "/health/ready"))

    run_id = f"trace-demo-{uuid4().hex[:8]}"
    agent_id = "trace-demo-agent"
    emit_model_turn(args.endpoint, run_id, agent_id)
    emit_tool_turn(args.endpoint, run_id, agent_id)

    print("\n== recent runtime sample ==")
    print_json(get_json(args.endpoint, "/v1/tools/recent?limit=1"))
    print("\nTrace file is written by the sidecar in CLAWTUNE_TRACE_DIR.")


def emit_model_turn(endpoint: str, run_id: str, agent_id: str) -> None:
    call_id = f"llm-{uuid4().hex[:8]}"
    messages = [
        {"role": "user", "content": "Run a heavier Python tool and report the result."}
    ]
    started_at = utc_now()
    post_json(
        endpoint,
        "/v1/events/model",
        {
            **common("evt-model-start", run_id, agent_id),
            "occurred_at": started_at,
            "event_type": "model_call_started",
            "call_id": call_id,
            "provider": "demo-provider",
            "model": "demo-model",
            "duration_ms": None,
            "outcome": None,
            "context_token_budget": 8192,
            "raw_input": messages,
            "raw_output": None,
            "raw_event": {"messages": messages},
        },
    )
    time.sleep(0.05)
    post_json(
        endpoint,
        "/v1/events/model",
        {
            **common("evt-model-end", run_id, agent_id),
            "event_type": "model_call_ended",
            "call_id": call_id,
            "provider": "demo-provider",
            "model": "demo-model",
            "duration_ms": 50,
            "outcome": "success",
            "context_token_budget": 8192,
            "raw_input": None,
            "raw_output": "I will run the heavier Python demo tool.",
            "raw_event": {"content": "I will run the heavier Python demo tool."},
        },
    )


def emit_tool_turn(endpoint: str, run_id: str, agent_id: str) -> None:
    tool_call_id = f"tool-{uuid4().hex[:8]}"
    output_path = ROOT / "data" / "trace-demo-tool-output.txt"
    binary_path = ROOT / "data" / "trace-demo-tool-payload.bin"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path\n"
            "import hashlib, math, os, time\n"
            f"p = Path({str(output_path)!r})\n"
            f"b = Path({str(binary_path)!r})\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "blob = bytearray(os.urandom(16 * 1024 * 1024))\n"
            "total = 0.0\n"
            "digest = hashlib.sha256()\n"
            "for _ in range(24):\n"
            "    for i in range(0, len(blob), 4096):\n"
            "        blob[i] = (blob[i] + 1) % 256\n"
            "    digest.update(blob)\n"
            "    total += sum(math.sqrt(i) for i in range(20000))\n"
            "b.write_bytes(blob)\n"
            "read_back = b.read_bytes()\n"
            "time.sleep(0.5)\n"
            "p.write_text(f'demo-tool-result total={total:.2f} bytes={len(read_back)} sha256={digest.hexdigest()[:16]}\\n', encoding='utf-8')\n"
            "print(f'demo-tool-result total={total:.2f} bytes={len(read_back)} sha256={digest.hexdigest()[:16]}')\n"
        ),
    ]
    output_path.unlink(missing_ok=True)
    binary_path.unlink(missing_ok=True)
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    scope = {
        "kind": "pid",
        "execution_id": None,
        "pid": process.pid,
        "root_pid": process.pid,
        "process_start_time": None,
        "root_starttime_ticks": None,
        "cgroup_path": None,
        "pid_namespace_inode": None,
        "container_id": None,
        "include_children": True,
        "source": "demo_trace_recorder",
        "attribution_source": "demo_trace_recorder",
    }
    request = {
        **common("evt-tool-before", run_id, agent_id),
        "tool_call_id": tool_call_id,
        "tool_name": "exec",
        "tool_kind": "shell",
        "tool_input_kind": "json",
        "operation_hint": "python",
        "derived_paths": [str(output_path), str(binary_path)],
        "params_digest": "sha256:" + "2" * 64,
        "param_features": {
            "serialized_size_bytes": 128,
            "string_length": 64,
            "list_item_count": 0,
            "path_count": 2,
            "has_command_like_field": True,
        },
        "raw_params": {
            "command": " ".join(command),
            "cwd": str(ROOT),
            "workload": "cpu-memory-disk",
        },
        "raw_event": {
            "params": {
                "command": " ".join(command),
                "cwd": str(ROOT),
                "workload": "cpu-memory-disk",
            }
        },
        "resource_scope": scope,
    }
    decision = post_json(endpoint, "/v1/decisions/tool", request)
    result_text = wait_for_text(output_path)
    duration_ms = int((time.monotonic() - started) * 1000)
    completion = {
        **common("evt-tool-after", run_id, agent_id),
        "tool_call_id": tool_call_id,
        "decision_id": decision["decision_id"],
        "lease_id": decision["lease_id"],
        "execution_id": None,
        "tool_name": "exec",
        "duration_ms": duration_ms,
        "succeeded": True,
        "error_type": None,
        "error_digest": None,
        "result_size_bytes": len(result_text.encode("utf-8")),
        "raw_result": {"stdout": result_text, "stderr": "", "exit_code": 0},
        "raw_event": {"result": {"stdout": result_text, "stderr": "", "exit_code": 0}},
        "resource_scope": scope,
    }
    print("\n== decision ==")
    print_json(decision)
    print("\n== completion ==")
    print_json(post_json(endpoint, "/v1/events/tool-completed", completion))
    if process.poll() is None:
        process.terminate()
    process.communicate(timeout=5)


def common(event_prefix: str, run_id: str, agent_id: str) -> dict[str, object]:
    return {
        "schema_version": "clawtune.v1",
        "event_id": f"{event_prefix}-{uuid4().hex[:8]}",
        "occurred_at": utc_now(),
        "plugin_version": "demo-trace-recorder",
        "run_id": run_id,
        "session_id": "trace-demo-session",
        "session_key": None,
        "agent_id": agent_id,
    }


def wait_for_text(path: Path, timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text:
                return text
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for demo tool output: {path}")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
