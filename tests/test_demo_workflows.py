from __future__ import annotations

import json
from pathlib import Path
import urllib.request
import urllib.error

import pytest

from benchmarks.bootstrap import ROOT
from benchmarks.adapters import ADAPTERS, NAMES, load, select
from clawtune_kb import FILES, StateStore, create_seed, initialize_state, validate_seed
from clawtune_kb.store import digest, write_json
from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB, CompletedCall, LatencyBuckets
from tool_time.lattice_kb import LatticeTimeKB


def make_seed(path):
    return create_seed(path, dict(zip(FILES, (ClauseResourceKB().to_json_obj(),
        RuntimeToolResourceKB().to_json_obj(), LatticeTimeKB.fit([]).to_json_obj()))), provenance={"test": True})


def test_peer_task_identity_and_verified_image(tmp_path):
    assert set(ADAPTERS) == set(NAMES)
    row = {"instance_id": "org__repo-1", "repo": "org/repo", "problem_statement": "fix"}
    a = ADAPTERS["swe-rebench"]({**row, "docker_image": "swerebench/task:latest"})
    b = ADAPTERS["swe-bench-verified"](row)
    assert a.key != b.key and a.kind == b.kind == "repository"
    assert b.image == "docker.io/swebench/sweb.eval.x86_64.org_1776_repo-1:latest"
    research = ADAPTERS["deep-research-bench"]({"id": 0, "prompt": "question", "article": "secret answer"})
    assert research.task_id == "0" and "secret" not in research.prompt
    bfcl = ADAPTERS["bfcl"]({"id": "multi_turn_base_1", "question": [[{"role": "user", "content": "work"}]],
                            "function": [], "involved_classes": ["FileSystem"]})
    assert bfcl.group == "multi_turn_base" and bfcl.kind == "functions"
    tb = tmp_path / "terminal-task"
    tb.mkdir()
    (tb / "task.yaml").write_text("instruction: solve this\ncategory: system\n")
    terminal = load("terminal-bench", tb)[0]
    assert terminal.kind == "terminal" and terminal.group == "system"


def test_selection_is_ordered_and_strict():
    tasks = [ADAPTERS["deep-research-bench"]({"id": i, "prompt": "q"}) for i in range(4)]
    assert [t.task_id for t in select(tasks, sample=2, skip=1)] == ["1", "2"]
    with pytest.raises(ValueError):
        select(tasks, sample=5)
    with pytest.raises(ValueError):
        select(tasks, sample=1, ids="unknown")


def test_seed_immutable_state_isolated_and_crash_restore(tmp_path):
    seed = tmp_path / "seed"
    make_seed(seed)
    seed_hash = digest(seed / "manifest.json")
    a, b = tmp_path / "a", tmp_path / "b"
    initialize_state(a, seed, owner="run:a")
    initialize_state(b, seed, owner="run:b")
    with StateStore(a) as store:
        kb = RuntimeToolResourceKB()
        kb.observe_completed_call(CompletedCall("repo", "read", None, 1, 2))
        write_json(a / FILES[1], kb.to_json_obj())
        state = store.checkpoint()
        assert state["generation"] == 2
        with pytest.raises(OSError):
            with StateStore(a):
                pass
    (a / FILES[1]).write_text("interrupted write")
    with StateStore(a):
        assert json.loads((a / FILES[1]).read_text())["pending"]
    assert json.loads((b / FILES[1]).read_text())["pending"] == []
    assert digest(seed / "manifest.json") == seed_hash
    validate_seed(seed)


def test_managed_predictor_learns_persists_and_reopens(tmp_path):
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    seed, state = tmp_path / "seed", tmp_path / "state"
    make_seed(seed)
    initialize_state(state, seed, owner="daily")
    def open_predictor():
        return ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
            buckets=LatencyBuckets((100, 1000)), artifact_dir=state)
    predictor = open_predictor()
    predictor.continuous_kb.observe_completed_call(CompletedCall("repo", "lookup", None, 1, 2))
    predictor._runtime_kb_version += 1
    predictor._kb_writes.enqueue(())
    predictor.close()
    predictor = open_predictor()
    from tool_resource.runtime_kb import ToolCallQuery
    assert predictor.continuous_kb.predict_load_samples(ToolCallQuery("repo", "lookup", None, 3))["duration_ms"]["values"] == (1000.,)
    predictor.close()


def write_trace(path, benchmark, task_id, duration=1000):
    rows = [{"type": "trace_metadata", "trace_format_version": 5,
             "instance_id": task_id, "benchmark": benchmark},
            {"type": "action", "action_type": "tool_exec", "instance_id": task_id, "action_id": "a",
             "data": {"tool_name": "lookup", "tool_args": "{}", "duration_ms": duration, "success": True}}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_all_datasets_offline_call_only_and_test_does_not_train(tmp_path):
    from offline.runner import inventory, split_tasks, run
    dataset = tmp_path / "input"
    dataset.mkdir()
    for name in NAMES:
        for index in range(5):
            task_id = f"org__repo-{index}" if name.startswith("swe-") else f"task-{index}"
            write_trace(dataset / f"{name}-{index}.jsonl", name, task_id)
    manifest = split_tasks(inventory(dataset.resolve(), None), 42)
    assert len(manifest["train"]) == 20 and len(manifest["test"]) == 5
    first = tmp_path / "first"
    report = run(dataset, first)
    assert report["test_updates"] == 0
    assert {metric["benchmark"] for metric in report["metrics"]} == set(NAMES)
    for task in manifest["test"]:
        entry = manifest["tasks"][task]
        for file in entry["files"]:
            write_trace(dataset / file["path"], entry["benchmark"], entry["task_id"], 90000)
    second = tmp_path / "second"
    run(dataset, second)
    assert all(digest(first / benchmark / "seed" / name) == digest(second / benchmark / "seed" / name)
               for benchmark in NAMES for name in FILES)


def test_bridge_auth_and_duplicate_mutation(tmp_path):
    from benchmarks.tool_bridge import ToolBridge
    class Backend:
        tools = [{"name": "increment", "parameters": {"type": "object"}}]
        count = 0
        def call(self, name, args):
            self.count += 1
            return self.count
    backend = Backend()
    with ToolBridge(backend) as bridge:
        url = f"http://127.0.0.1:{bridge.server.server_port}/call"
        data = json.dumps({"name": "increment", "call_id": "one", "arguments": {}}).encode()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=2)
        for _ in range(2):
            request = urllib.request.Request(url, data=data, headers={"Authorization": "Bearer " + bridge.token})
            with urllib.request.urlopen(request, timeout=2) as response:
                assert json.load(response)["result"] == 1
    assert backend.count == 1


def test_seed_cannot_be_opened_for_learning(tmp_path):
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    make_seed(tmp_path / "seed")
    with pytest.raises(ValueError, match="immutable seed"):
        ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
            buckets=LatencyBuckets((100, 1000)), artifact_dir=tmp_path / "seed")


def test_offline_scores_pmu_separately_from_call_load(monkeypatch, tmp_path):
    from offline import runner
    from cold_start.flat_loader import LoadedTask
    source = tmp_path / "input"
    source.mkdir()
    for i in range(5):
        write_trace(source / f"{i}.jsonl", "bfcl", str(i))
    def load(*args):
        return LoadedTask(calls=[CompletedCall("bfcl:dataset", "lookup", None, 0, 1,
            pmu_eligible=True, pmu_ipc=.5, pmu_llc_mpki=1., pmu_llc_miss_rate=.1)])
    monkeypatch.setattr(runner, "load_task", load)
    report = runner.run(source, tmp_path / "output")
    metrics = {row["target"]: row for row in report["metrics"]}
    assert {"pmu_ipc", "pmu_llc_mpki", "pmu_llc_miss_rate"} <= metrics.keys()
    assert all(row["mae"] == 0 for row in metrics.values())
    assert report["test_updates"] == 0


def test_bfcl_preserves_native_state_and_turns(monkeypatch, tmp_path):
    import sys
    from types import ModuleType, SimpleNamespace
    from benchmarks.backends import BFCLBackend
    monkeypatch.delenv("BFCL_REPO_PATH", raising=False)
    class Counter:
        total = 4
        def increment(self, count):
            self.total += count
            return self.total
    counter = Counter()
    def native_start(calls, config, classes, model, task_id, **kwargs):
        assert calls == [] and config == {"Counter": {"total": 4}}
        return [], {"Counter": counter}
    modules = {
        "bfcl_eval.eval_checker.multi_turn_eval.multi_turn_utils": {"execute_multi_turn_func_call": native_start},
        "bfcl_eval.model_handler.utils": {"convert_to_tool": lambda functions, *args: [{"function": f} for f in functions]},
        "bfcl_eval.constants.enums": {"ModelStyle": SimpleNamespace(OPENAI_COMPLETIONS="openai")},
        "bfcl_eval.constants.type_mappings": {"GORILLA_TO_OPENAPI": {}},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    entry = {"id": "multi_turn_base_1", "initial_config": {"Counter": {"total": 4}},
        "involved_classes": ["Counter"], "function": [{"name": "increment", "parameters": {"type": "object"}}],
        "question": [[{"role": "system", "content": "Keep state"}, {"role": "user", "content": "Add two"}],
                     [{"role": "user", "content": "Add three"}]]}
    backend = BFCLBackend(ADAPTERS["bfcl"](entry), tmp_path)
    assert backend.system == ["Keep state"] and backend.turns == ["Add two", "Add three"]
    assert backend.call("increment", {"count": 2}) == 6
    assert backend.call("increment", {"count": 3}) == 9
    assert entry["initial_config"]["Counter"]["total"] == 4
    backend.close()


def test_terminal_copies_native_environment_and_rejects_external_bind(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from benchmarks.backends import TerminalBackend
    taskdir = tmp_path / "input/task"
    taskdir.mkdir(parents=True)
    (taskdir / "task.yaml").write_text("instruction: work\n")
    (taskdir / "docker-compose.yaml").write_text("services: {client: {image: test}}\n")
    before = {p.name: digest(p) for p in taskdir.iterdir()}
    output = tmp_path / "run"
    output.mkdir()
    calls = []
    def invoke(self, args, **kwargs):
        assert self.root.is_relative_to(output) and self.root != taskdir
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps({"services": {"client": {"volumes": [{"type": "bind", "source": str(taskdir)}]}}}))
    monkeypatch.setattr(TerminalBackend, "_run", invoke)
    with pytest.raises(ValueError, match="outside its run directory"):
        TerminalBackend(load("terminal-bench", taskdir)[0], output)
    assert calls == [["config", "--format", "json"]]
    assert {p.name: digest(p) for p in taskdir.iterdir()} == before


def test_runner_shares_one_kb_and_resumes_only_saved_boundaries(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from benchmarks import runner, runtime
    from swe_rebench import config, prepare, host_openclaw, runner as old_runner
    seed = tmp_path / "seed"
    make_seed(seed)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm: {}\n")
    cfg = SimpleNamespace(llm=SimpleNamespace(api_key="test", model="test"),
        runtime=SimpleNamespace(), batch=SimpleNamespace(), output=SimpleNamespace())
    monkeypatch.setattr(config.RunnerConfig, "from_yaml", lambda *a, **k: cfg)
    monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prepare, "build_runtime_assets", lambda cfg: tmp_path)
    starts, stops, observed = [], [], []
    monkeypatch.setattr(host_openclaw, "_start_sidecar", lambda **k: starts.append(k) or "process")
    monkeypatch.setattr(host_openclaw, "_stop_process", lambda p: stops.append(p))
    def execute(task, cfg, assets, folder, port):
        path = folder / "kb"
        with StateStore(path) as store:
            observed.append(json.loads((path / "state.json").read_text())["generation"])
            kb = RuntimeToolResourceKB()
            kb.observe_completed_call(CompletedCall("repo", "read", None, 0, len(observed)))
            write_json(path / FILES[1], kb.to_json_obj())
            store.checkpoint()
        return SimpleNamespace(task_id=task.task_id, exit_code=0)
    monkeypatch.setattr(runtime, "execute", execute)
    monkeypatch.setattr(old_runner, "_result_dict", lambda result: {"task_id": result.task_id,
        "exit_code": result.exit_code, "error": None, "resource_summary": {"tool_span_ends": 1}})
    tasks = [ADAPTERS["deep-research-bench"]({"id": i, "prompt": "work"}) for i in range(2)]
    result = runner.run(tasks, config_path=cfg_path, seed=seed, output=tmp_path / "run")
    assert result["status"] == "completed" and observed == [1, 2]
    assert len(starts) == 1 and stops == ["process"]
    assert all(row["learning_status"] == "updated" for row in result["results"])
    runner.run(tasks, config_path=cfg_path, seed=seed, resume=tmp_path / "run")
    assert len(starts) == 1
    result["active_task"] = tasks[0].task_id
    write_json(tmp_path / "run/run.json", result)
    with pytest.raises(ValueError, match="partial learning"):
        runner.run(tasks, config_path=cfg_path, seed=seed, resume=tmp_path / "run")
