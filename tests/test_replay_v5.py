import json
import subprocess
import sys
import urllib.request
from pathlib import Path
import pytest
from swe_rebench.replay import load_replay_plan, ReplayTraceError, ReplayLLMServer
from swe_rebench.replay_v5 import adapt_v5_plan
from swe_rebench.replay_files import execute


def test_v5_streaming_and_serial_dispatch_preserve_calls_and_delay(tmp_path):
    rows = actions()
    calls = rows[1]["data"]["raw_response"]["choices"][0]["message"]["tool_calls"]
    calls.append({"id": "call-B", "function": {"name": "read_file", "arguments": {"path": "/testbed/file"}}})
    source = load_replay_plan(write(tmp_path / "v5.jsonl", rows))
    runtime = adapt_v5_plan(source)
    assert source.incomplete_tool_count == 1
    assert [c.span_id for t in runtime.turns for c in t.tool_calls] == ["call-A", "call-B"]
    assert [t.duration_ns for t in runtime.turns] == [250000000, 0, 0]
    server = ReplayLLMServer(runtime, timing="none")
    server.start()
    try:
        for turn in runtime.turns:
            request = urllib.request.Request(server.base_url + "/chat/completions",
                data=json.dumps({"stream": True}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                payload = response.read().decode()
            delta = json.loads(payload.splitlines()[0][6:])["choices"][0]["delta"]
            if turn.tool_calls:
                assert delta["tool_calls"][0]["index"] == 0
                assert delta["tool_calls"][0]["id"] == turn.tool_calls[0].span_id
            else:
                assert "tool_calls" not in delta
    finally:
        server.close()


def test_v6_runtime_is_not_adapted():
    source = load_replay_plan(Path("packages/clawtune-plugin/test/fixtures/trace_v6_sample.jsonl"))
    assert adapt_v5_plan(source) is source
    assert [len(t.tool_calls) for t in source.turns] == [2, 1]
    assert load_replay_plan(source.source_paths[0], task_id="other-task") == source


def test_v5_rejects_multiple_attempts_and_unknown_tools(tmp_path):
    rows = actions()
    with pytest.raises(ReplayTraceError, match="duplicate action"):
        load_replay_plan(write(tmp_path / "duplicate.jsonl", rows + rows[1:]))
    rows[1]["data"]["raw_response"]["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "unknown"
    rows[2]["data"]["tool_name"] = "unknown"
    with pytest.raises(ReplayTraceError, match="unsupported v5 replay tool"):
        adapt_v5_plan(load_replay_plan(write(tmp_path / "unknown.jsonl", rows)))


def actions(*, version=5, task="task", encoded=True, placeholder=False):
    args = {"command": "printf hello", "working_dir": "/testbed/sub dir", "timeout": 15}
    return [
        {"type": "trace_metadata", "trace_format_version": version, "instance_id": task, "model": "recorded-model"},
        {"type": "action", "action_type": "llm_call", "action_id": "model-A", "iteration": 0,
         "ts_start": 1., "ts_end": 1.25, "data": {"raw_response": {"choices": [{"message": {
             "role": "assistant", "content": None, "tool_calls": [{"id": "placeholder" if placeholder else "call-A",
             "type": "function", "function": {"name": "exec", "arguments": json.dumps(args) if encoded else args}}]}}]}}},
        {"type": "action", "action_type": "tool_exec", "action_id": "tool-A", "iteration": 0,
         "ts_start": 1.3, "ts_end": 5.4, "data": {"tool_name": "exec", "tool_call_id": "call-A",
             "tool_args": json.dumps(args) if encoded else args}},
    ]

def write(path, records, *, bom=False):
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8-sig" if bom else "utf-8")
    return path

@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("placeholder", [False, True])
def test_v5_preserves_actual_ids_arguments_and_model_duration(tmp_path, encoded, placeholder):
    path = write(tmp_path / "old.jsonl", actions(encoded=encoded, placeholder=placeholder), bom=True)
    plan = load_replay_plan(path)
    assert plan.source_format == "v5"
    assert plan.tool_count == 1 and plan.incomplete_tool_count == 0
    assert plan.turns[0].duration_ns == 250000000
    assert plan.turns[0].content is None
    assert plan.turns[0].tool_calls[0].span_id == "call-A"
    adapted = adapt_v5_plan(plan)
    assert adapted.turns[0].tool_calls[0].arguments == {
        "command": "cd -- '/testbed/sub dir' || exit $?\nprintf hello", "timeout": 15}
    assert plan.turns[0].tool_calls[0].arguments["command"] == "printf hello"
    assert not adapted.turns[-1].tool_calls

def test_missing_executed_call_is_not_silently_dropped(tmp_path):
    plan = load_replay_plan(write(tmp_path / "trace.jsonl", actions()[:2]))
    assert plan.tool_count == plan.incomplete_tool_count == 1

def test_identical_requests_are_matched_once_each(tmp_path):
    rows = actions(placeholder=True)
    calls = rows[1]["data"]["raw_response"]["choices"][0]["message"]["tool_calls"]
    calls.append({**calls[0], "id": "placeholder-2"})
    rows.append({**rows[2], "action_id": "tool-B", "ts_start": 6., "ts_end": 7.,
                 "data": {**rows[2]["data"], "tool_call_id": "call-B"}})
    plan = load_replay_plan(write(tmp_path / "trace.jsonl", rows))
    assert [c.span_id for c in plan.turns[0].tool_calls] == ["call-A", "call-B"]
    assert plan.tool_count == 2 and plan.incomplete_tool_count == 0

def test_mismatched_arguments_fail_instead_of_duplicating_execution(tmp_path):
    rows = actions()
    rows[2]["data"]["tool_args"] = '{"command":"different"}'
    with pytest.raises(ReplayTraceError, match="differs"):
        load_replay_plan(write(tmp_path / "trace.jsonl", rows))

def test_directory_selects_task_and_ignores_manifest(tmp_path):
    selected = write(tmp_path / "a.trace.jsonl", actions(task="a"))
    write(tmp_path / "b.trace.jsonl", actions(task="b"))
    write(tmp_path / "MANIFEST.jsonl", [{"instance_id": "a", "source_path": "source"}])
    assert load_replay_plan(tmp_path, task_id="a").source_paths == (selected,)
    with pytest.raises(ReplayTraceError, match="multiple"):
        load_replay_plan(tmp_path)
    with pytest.raises(ReplayTraceError, match="does not match"):
        load_replay_plan(selected, task_id="b")

def test_legacy_file_edits_preserve_crlf_fallback_and_replace_all(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"  old  \r\n  old  \r\n")
    args = {"path": str(path), "old_text": "old", "new_text": "new"}
    assert execute("edit_file", args).startswith("Warning:")
    assert path.read_bytes() == b"  old  \r\n  old  \r\n"
    assert execute("edit_file", {**args, "replace_all": True}).startswith("Successfully")
    assert path.read_bytes() == b"  new  \r\n  new  \r\n"
    path.write_bytes(b"  first  \r\n second \r\n")
    assert execute("edit_file", {**args, "old_text": "first\nsecond", "new_text": "changed"}).startswith("Successfully")
    assert path.read_bytes() == b"changed\r\n"

def test_legacy_write_read_and_list_maintain_file_state(tmp_path):
    path = tmp_path / "nested/file.txt"
    assert execute("write_file", {"path": str(path), "content": "one\ntwo\n"}).startswith("Successfully")
    assert execute("read_file", {"path": str(path), "offset": 2, "limit": 1}).startswith("2| two")
    assert "nested/file.txt" in execute("list_dir", {"path": str(tmp_path), "recursive": True})
    assert execute("write_file", {"content": "bad"}).startswith("Error:")

def test_file_helper_runs_as_standalone_task_python(tmp_path):
    helper = Path("swe_rebench/replay_files.py").read_text(encoding="utf-8")
    request = {"name": "write_file", "arguments": {"path": "file.txt", "content": "hello"}}
    result = subprocess.run([sys.executable, "-c", helper], input=json.dumps(request),
                            text=True, capture_output=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "file.txt").read_text() == "hello"

def test_legacy_runtime_mount_and_tool_policy_are_replay_only(tmp_path):
    from swe_rebench.config import RunnerConfig
    from swe_rebench.host_openclaw import _openclaw_config
    config_file = tmp_path / "config.yaml"
    config_file.write_text("")
    config = RunnerConfig.from_yaml(config_file, repo_root=tmp_path)
    kwargs = dict(endpoint_host="http://localhost:1", endpoint_sandbox="http://host:1",
                  workspace=tmp_path / "task", config=config)
    normal = json.loads(_openclaw_config(**kwargs))
    assert "binds" not in normal["agents"]["defaults"]["sandbox"]["docker"]
    config.replay_legacy = True
    config.replay_tool_names = ["exec", "edit_file"]
    replay = json.loads(_openclaw_config(**kwargs))
    assert replay["agents"]["defaults"]["sandbox"]["docker"]["binds"] == [f"{tmp_path / 'task'}:/testbed:rw"]
    assert replay["tools"]["sandbox"]["tools"]["allow"] == ["exec", "edit_file"]

def test_legacy_tool_bridge_executes_only_in_unique_sandbox(monkeypatch, tmp_path):
    from swe_rebench import host_openclaw as host
    from swe_rebench import replay_tools
    from benchmarks.tool_bridge import ToolBridge
    monkeypatch.setattr(host, "_require_executable", lambda name: name)
    containers = ["task-container"]
    monkeypatch.setattr(host, "_docker_sandbox_container_ids", lambda *a: containers)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps("file result"), "")
    monkeypatch.setattr(replay_tools, "run_command", run)
    backend = replay_tools.LegacyReplayTools(workspace=tmp_path, deadline=None,
        names={"read_file", "clawtune_replay_message"})
    with ToolBridge(backend) as bridge:
        manifest = tmp_path / "bridge.json"
        bridge.manifest(manifest)
        config = json.loads(manifest.read_text())
        request = urllib.request.Request(config["endpoint"], data=json.dumps({
            "name": "read_file", "call_id": "read-1", "arguments": {"path": "/tmp/source"}}).encode(),
            headers={"Authorization": "Bearer " + config["token"]})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["result"] == "file result"
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["result"] == "file result"
        assert len(calls) == 1  # Duplicate delivery cannot repeat an edit/write.
        assert calls[0][0][:6] == ["docker", "exec", "-i", "--workdir", "/testbed", "task-container"]
        assert json.loads(calls[0][1]["input"])["arguments"]["path"] == "/tmp/source"
        assert backend.call("clawtune_replay_message", {"content": "local"}) == "local"
        assert len(calls) == 1
        containers.clear()
        with pytest.raises(RuntimeError, match="exactly one"):
            backend.call("read_file", {"path": str(tmp_path / "host-secret")})
