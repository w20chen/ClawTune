from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _write_attempt(run_dir: Path) -> Path:
    attempt = run_dir / "case-1" / "attempt_1"
    attempt.mkdir(parents=True)
    (attempt / "trace.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "trace_metadata",
                        "trace_format_version": 5,
                        "run_id": "bench_run_1",
                        "benchmark": "swe-rebench",
                        "scaffold": "openclaw",
                    }
                ),
                json.dumps(
                    {
                        "type": "action",
                        "action_type": "tool_exec",
                        "agent_id": "agent-1",
                        "action_id": "tool-1",
                        "ts_start": 10.0,
                        "ts_end": 12.0,
                        "data": {
                            "tool_name": "exec",
                            "tool_args": json.dumps(
                                {"command": "cd /testbed && python3 -m pytest tests/"}
                            ),
                            "success": True,
                        },
                    }
                ),
                json.dumps({"type": "summary", "agent_id": "agent-1"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (attempt / "run_manifest.json").write_text(
        json.dumps(
            {
                "task": {
                    "instance_id": "case-1",
                    "docker_image": "swerebench/sweb.eval.x86_64.case:latest",
                },
                "replay": {
                    "source_image": "swerebench/sweb.eval.x86_64.case:latest",
                    "fixed_image_name": "agent-test-bench-fixed:case",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (attempt / "resources.json").write_text('{"samples":[],"summary":{}}\n', encoding="utf-8")
    return attempt


def test_validate_agent_test_bench_run_outputs_events_profiles_and_images(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "run"
    _write_attempt(run_dir)
    events = tmp_path / "events.jsonl"
    profiles = tmp_path / "profiles.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "validate_agent_test_bench_run.py"),
            str(run_dir),
            "--events-out",
            str(events),
            "--profiles-out",
            str(profiles),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )

    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["trace_count"] == 1
    assert report["tool_exec_count"] == 1
    assert "swerebench/sweb.eval.x86_64.case:latest" in report["images"]

    event = json.loads(events.read_text(encoding="utf-8").splitlines()[0])
    assert event["duration_ms"] == 2000
    assert event["tool_name"] == "exec"

    profile = json.loads(profiles.read_text(encoding="utf-8"))
    assert profile["profiles"][0]["operation"] == "pytest"


def test_run_agent_test_bench_dry_run_delegates_original_cli(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    bench_root = tmp_path / "agent-test-bench"
    bench_root.mkdir()

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "run_agent_test_bench.py"),
            "--bench-root",
            str(bench_root),
            "--dry-run",
            "--",
            "--provider",
            "deepseek",
            "--model",
            "deepseek-v4-flash",
            "--benchmark",
            "swe-rebench",
            "--scaffold",
            "openclaw",
            "--container",
            "docker",
            "--mcp-config",
            "none",
            "--sample",
            "1",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["cwd"] == str(bench_root)
    assert payload["command"][1:3] == ["-m", "trace_collect.cli"]
    assert payload["command"][-2:] == ["--sample", "1"]
    assert str(bench_root / "src") in payload["PYTHONPATH"]
