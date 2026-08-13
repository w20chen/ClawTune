from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_importer_outputs_schema_shaped_events_and_profiles(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    trace = root / "tests" / "fixtures" / "agent_test_bench_trace.jsonl"
    events = tmp_path / "events.jsonl"
    profiles = tmp_path / "profiles.json"
    subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "import_agent_test_bench_trace.py"),
            str(trace),
            str(events),
            "--profiles-out",
            str(profiles),
        ],
        check=True,
        cwd=root,
    )
    event = json.loads(events.read_text(encoding="utf-8").splitlines()[0])
    assert event["schema_version"] == "clawtune.v1"
    assert event["run_id"] == "bench_run_1"
    assert event["tool_name"] == "exec-pytest"
    assert event["duration_ms"] == 1500
    assert "source_trace_id" not in event

    profile = json.loads(profiles.read_text(encoding="utf-8"))
    assert profile["profiles"][0]["operation"] == "pytest"
    assert profile["profiles"][0]["duration_p50_ms"] == 1500
