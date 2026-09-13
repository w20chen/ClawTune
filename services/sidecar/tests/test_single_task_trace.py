from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from clawtune_sidecar.trace import AgentTestBenchTraceWriter
from clawtune_sidecar.contracts.models import ModelEvent


def writer_at(root):
    target = root / "traces" / "task" / "trace.jsonl"
    return AgentTestBenchTraceWriter(root / "sidecar", runtime_paths={json.dumps(["g", "r"]): str(target)}), target


def event(index, phase, text="hello"):
    return ModelEvent(schema_version="clawtune.v1", event_id=f"{index}-{phase}",
        occurred_at="2026-09-13T00:00:00Z", plugin_version="0.1.0",
        gateway_id="g", runtime_id="r", run_id=f"run-{index}", session_id=f"session-{index}",
        session_key=None, agent_id=f"agent-{index}", event_type=f"model_call_{phase}",
        call_id=f"call-{index}", provider="test", model="test", duration_ms=1,
        outcome="success" if phase == "ended" else None,
        raw_input=[{"role": "user", "content": text}] if phase == "started" else None,
        raw_output="done" if phase == "ended" else None)


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]


def test_sessions_agents_and_concurrent_runs_share_one_complete_file(tmp_path):
    writer, path = writer_at(tmp_path)
    large = "complete context " * 15000
    def run(index):
        writer.record_model(event(index, "started", large))
        writer.record_model(event(index, "ended"))
        assert writer.flush()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(16)))
    writer.close()
    rows = records(path)
    assert list(tmp_path.rglob("*.jsonl")) == [path]
    assert len(rows) == 33
    assert sum(r["record_type"] == "trace_metadata" for r in rows) == 1
    starts = [r for r in rows if r["record_type"] == "span_start"]
    assert {r["sequence_no"] for r in starts} == set(range(1, 17))
    assert all(r["input"]["messages"][0]["content"] == large for r in starts)


def test_late_telemetry_and_pending_model_are_retained_once(tmp_path):
    writer, path = writer_at(tmp_path)
    writer.record_model(event(1, "started"))
    writer.release_runtime("r", "g")
    artifact = tmp_path / "kb" / "execution.json"
    artifact.parent.mkdir()
    payload = {"execution_id": "exec-1", "clauses": [{"cpu_time_ns": 123, "raw": [1, 2, 3]}]}
    artifact.write_text(json.dumps(payload), encoding="utf8")
    for _ in range(2):
        writer.record_tool_resource_telemetry("exec-1", {"artifact_path": str(artifact)}, gateway_id="g", runtime_id="r")
    writer.close()
    rows = records(path)
    assert len(rows) == 3
    assert rows[1]["event_type"] == "incomplete_span"
    assert rows[2]["artifact"] == payload
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "trace-event.schema.json").read_text(encoding="utf8"))
    for row in rows[1:]:
        Draft202012Validator(schema).validate(row)
    assert list((tmp_path / "traces").rglob("*.json*")) == [path]


def test_write_failure_is_not_acknowledged(tmp_path):
    writer, path = writer_at(tmp_path)
    path.parent.mkdir(parents=True)
    path.mkdir()  # Cannot append to a directory.
    writer.record_model(event(1, "started"))
    writer.record_model(event(1, "ended"))
    assert not writer.flush()
    with pytest.raises(RuntimeError, match="persistence"):
        writer.close()


def test_duplicate_task_destinations_are_rejected(tmp_path):
    path = str(tmp_path / "trace.jsonl")
    with pytest.raises(ValueError, match="cannot share"):
        AgentTestBenchTraceWriter(tmp_path, runtime_paths={json.dumps(["g", "r"]): path, json.dumps(["g", "other"]): path})


def test_restart_does_not_duplicate_metadata(tmp_path):
    for index in range(2):
        writer, path = writer_at(tmp_path)
        writer.record_model(event(index, "started"))
        writer.record_model(event(index, "ended"))
        writer.close()
    assert sum(r["record_type"] == "trace_metadata" for r in records(path)) == 1


def test_proxy_without_hook_is_not_lost_on_close(tmp_path):
    writer, path = writer_at(tmp_path)
    writer.record_llm_proxy_call(runtime_id="r", action_id="proxy-1", provider="test", model="test",
        messages_in=[{"role": "user", "content": "context"}], content="response",
        raw_request={"model": "test"}, raw_response={"usage": {"total_tokens": 123}},
        ts_start=1.0, ts_end=2.0, status_code=200, stream=False)
    writer.close()
    row = records(path)[1]
    assert row["event_type"] == "llm_proxy_unmatched"
    assert row["payload"]["data"]["content"] == "response"
    assert row["payload"]["data"]["raw_response"]["usage"]["total_tokens"] == 123
    assert list(tmp_path.rglob("*.jsonl")) == [path]


def test_task_trace_suppresses_separate_proxy_debug_dump(tmp_path):
    from clawtune_sidecar.config import SidecarConfig
    from clawtune_sidecar.llm_proxy import _write_proxy_debug
    config = SidecarConfig(trace_dir=tmp_path, llm_proxy_debug_dump=True,
        trace_runtime_paths={json.dumps(["g", "r"]): str(tmp_path / "trace.jsonl")})
    _write_proxy_debug(config, action_id="p", upstream="test", payload={}, status_code=200,
        chunk_count=0, message={}, raw_preview=b"content", error="upstream_empty_response")
    assert list(tmp_path.iterdir()) == []


def test_reuses_handle_and_skips_redundant_flush(tmp_path, monkeypatch):
    writer, path = writer_at(tmp_path)
    opened = []
    synced = []
    original_open = Path.open
    def tracked_open(target, *args, **kwargs):
        if target == path:
            opened.append(target)
        return original_open(target, *args, **kwargs)
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr("clawtune_sidecar.trace.os.fsync", lambda fd: synced.append(fd))
    for index in range(3):
        writer.record_model(event(index, "started"))
        writer.record_model(event(index, "ended"))
        assert writer.flush()
        assert writer.flush()
    writer.close()
    assert len(opened) == 1
    assert len(synced) == 3


def test_joined_proxy_preserves_full_provider_content_and_metadata(tmp_path):
    from datetime import datetime, timezone
    writer, path = writer_at(tmp_path)
    timestamp = datetime(2026, 9, 13, tzinfo=timezone.utc).timestamp()
    full_content = [{"type": "text", "text": "complete answer"}]
    writer.record_model(event(1, "started"))
    writer.record_llm_proxy_call(runtime_id="r", action_id="proxy-1", provider="test", model="test",
        messages_in=[{"role": "user", "content": "actual provider context"}], content=full_content,
        raw_request={"model": "test", "temperature": 0.2},
        raw_response={"usage": {"total_tokens": 123}, "choices": [{"finish_reason": "stop",
            "message": {"role": "assistant", "content": full_content, "reasoning_content": "reason"}}]},
        ts_start=timestamp-1, ts_end=timestamp, status_code=200, stream=False)
    writer.record_model(event(1, "ended"))
    writer.close()
    rows = records(path)
    assert len(rows) == 3
    assert rows[1]["input"]["messages"][0]["content"] == "actual provider context"
    assert rows[1]["input"]["request_options"]["temperature"] == 0.2
    assert rows[2]["output"]["content"] == full_content
    metadata = rows[2]["output"]["provider_metadata"]
    assert metadata["usage"]["total_tokens"] == 123
    assert metadata["choices"][0]["message"]["reasoning_content"] == "reason"
    assert "content" not in metadata["choices"][0]["message"]
