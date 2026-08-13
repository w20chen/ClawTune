from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from swe_rebench.replay import ReplayLLMServer, ReplayTraceError, load_replay_plan


FIXTURE = Path("packages/clawtune-plugin/test/fixtures/trace_v6_sample.jsonl")


def test_load_replay_plan_extracts_v6_turns_and_tools() -> None:
    plan = load_replay_plan(FIXTURE)

    assert plan.trace_id == "run-1"
    assert len(plan.turns) == 2
    assert plan.tool_count == 3
    assert plan.incomplete_tool_count == 1
    assert [len(turn.tool_calls) for turn in plan.turns] == [2, 1]
    assert plan.turns[0].tool_calls[0].arguments["command"] == "ls -la /data"


def test_load_replay_plan_rejects_non_v6(tmp_path: Path) -> None:
    trace = tmp_path / "old.jsonl"
    trace.write_text(json.dumps({"type": "trace_metadata"}) + "\n", encoding="utf-8")

    with pytest.raises(ReplayTraceError, match="schema_version=6"):
        load_replay_plan(trace)


def test_replay_llm_server_sleeps_and_returns_recorded_tool_calls() -> None:
    plan = load_replay_plan(FIXTURE)
    server = ReplayLLMServer(plan, timing="none")
    server.start()
    try:
        import urllib.request

        request = urllib.request.Request(
            f"{server.base_url}/chat/completions",
            data=json.dumps({"model": plan.model, "messages": []}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
        assert time.monotonic() - started < 1.0
        message = payload["choices"][0]["message"]
        assert len(message["tool_calls"]) == 2
        assert message["tool_calls"][0]["function"]["name"] == "exec"
        assert server.requests == 1
    finally:
        server.close()
