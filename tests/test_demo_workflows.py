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
    (tb / "Dockerfile").write_text("FROM alpine\n")
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
    report = run(dataset, first, split_cache_dir=tmp_path / "splits")
    assert report["test_updates"] == 0
    assert {metric["benchmark"] for metric in report["metrics"]} == set(NAMES)
    assert len(report["repositories"]) == len(NAMES)
    assert all(repo["train_tasks"] == 4 and repo["test_tasks"] == 1
               for repo in report["repositories"])
    assert all(repo["metrics"][0]["bucket_metrics"]["accuracy"] == 1
               for repo in report["repositories"])
    for task in manifest["test"]:
        entry = manifest["tasks"][task]
        for file in entry["files"]:
            write_trace(dataset / file["path"], entry["benchmark"], entry["task_id"], 90000)
    second = tmp_path / "second"
    run(dataset, second)
    assert all(digest(first / benchmark / "seed" / name) == digest(second / benchmark / "seed" / name)
               for benchmark in NAMES for name in FILES)


def test_nested_dataset_prefers_final_trace_and_keeps_raw_only_attempt(tmp_path):
    from offline.runner import inventory
    dataset = tmp_path / "nested"
    final_attempt = dataset / "org__repo-1" / "attempt_1"
    raw_dir = final_attempt / "_task_container_runtime" / "openclaw"
    raw_dir.mkdir(parents=True)
    write_trace(final_attempt / "trace.jsonl", "swe-rebench", "org__repo-1", 1000)
    write_trace(raw_dir / "trace.raw.jsonl", "swe-rebench", "org__repo-1", 2000)
    raw_only = dataset / "org__repo-2" / "attempt_1" / "_task_container_runtime" / "openclaw"
    raw_only.mkdir(parents=True)
    write_trace(raw_only / "trace.raw.jsonl", "swe-rebench", "org__repo-2", 3000)

    excluded = []
    tasks = inventory(dataset.resolve(), "swe-rebench", excluded)

    assert len(tasks) == 2
    assert tasks["swe-rebench:org__repo-1"]["files"][0]["path"] == "org__repo-1/attempt_1/trace.jsonl"
    assert tasks["swe-rebench:org__repo-2"]["files"][0]["path"].endswith("trace.raw.jsonl")
    assert excluded == [{
        "path": "org__repo-1/attempt_1/_task_container_runtime/openclaw/trace.raw.jsonl",
        "reason": "preferred canonical trace org__repo-1/attempt_1/trace.jsonl in this attempt",
    }]


def test_first_use_split_registry_is_reused_by_task_roster(tmp_path):
    from offline.runner import inventory, load_or_create_split
    first_source, moved_source = tmp_path / "first", tmp_path / "moved"
    first_source.mkdir()
    moved_source.mkdir()
    for index in range(5):
        write_trace(first_source / f"{index}.jsonl", "bfcl", f"task-{index}")
        write_trace(moved_source / f"copy-{index}.jsonl", "bfcl", f"task-{index}", duration=9000)
    cache = tmp_path / "split-cache"

    first = load_or_create_split(inventory(first_source, None), first_source, 42, cache)
    reused = load_or_create_split(inventory(moved_source, None), moved_source, 42, cache)
    seventy = load_or_create_split(inventory(first_source, None), first_source, 42, cache, .7)

    assert first["split_registry_status"] == "created"
    assert reused["split_registry_status"] == "reused"
    assert reused["train"] == first["train"] and reused["test"] == first["test"]
    assert reused["registered_source_path"] == str(first_source)
    assert reused["current_source_path"] == str(moved_source)
    assert reused["split_registry_path"] == first["split_registry_path"]
    assert reused["assignment_sha256"] == first["assignment_sha256"]
    assert len(seventy["train"]) == 3 and len(seventy["test"]) == 2
    assert seventy["split_registry_path"] != first["split_registry_path"]
    assert seventy["assignment_sha256"] != first["assignment_sha256"]


def test_split_rejects_invalid_train_fraction(tmp_path):
    from offline.runner import split_tasks
    with pytest.raises(ValueError, match="greater than 0 and less than 1"):
        split_tasks({"bfcl:1": {"benchmark": "bfcl", "group": "dataset"}}, 42, 1.0)


def test_offline_metric_summary_reports_continuous_errors_and_bucket_recall():
    from offline.runner import _metric_summary
    rows = [
        {"task": "a", "unit": "ms", "actual": 50., "p50": 60., "p90": 80.,
         "baseline_p50": 55., "actual_bucket": 0, "predicted_bucket": 0,
         "bucket_probabilities": [.8, .1, .1]},
        {"task": "b", "unit": "ms", "actual": 200., "p50": 600., "p90": 700.,
         "baseline_p50": 250., "actual_bucket": 1, "predicted_bucket": 2,
         "bucket_probabilities": [.1, .2, .7]},
        {"task": "b", "unit": "ms", "actual": 300., "p50": 250., "p90": 350.,
         "baseline_p50": 250., "actual_bucket": 1, "predicted_bucket": 1,
         "bucket_probabilities": [.1, .8, .1]},
    ]

    result = _metric_summary("bfcl", "duration_ms", rows, (100., 500.))

    assert result["mae"] == pytest.approx(460 / 3)
    assert result["rmse"] == pytest.approx((10**2 + 400**2 + 50**2) ** .5 / 3**.5)
    assert result["mean_error_bias"] == 120
    assert result["bucket_metrics"]["accuracy"] == pytest.approx(2 / 3)
    assert result["bucket_metrics"]["macro_recall"] == pytest.approx(.75)
    assert result["bucket_metrics"]["per_bucket"][1]["recall"] == pytest.approx(.5)


def test_recorded_single_clause_can_bypass_missing_offline_parser():
    from offline.runner import _safe_recorded_clause
    clause = ({"bin": "python", "argv": ("/usr/bin/python", "-m", "pytest"),
               "in_loop": False, "in_pipe": False, "in_subst": False,
               "pipeline_position": -1},)
    assert _safe_recorded_clause("/usr/bin/python -m pytest", clause) == clause
    assert _safe_recorded_clause("/usr/bin/python -m pytest | head", clause) is None


def test_bridge_auth_and_duplicate_mutation(tmp_path):
    from benchmarks.tool_bridge import ToolBridge
    class Backend:
        tools = [{"name": "increment", "parameters": {"type": "object"}}]
        count = 0
        def call(self, name, args, *, call_id=""):
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
    report = runner.run(source, tmp_path / "output", split_cache_dir=tmp_path / "splits")
    metrics = {row["target"]: row for row in report["metrics"]}
    assert {"pmu_ipc", "pmu_llc_mpki", "pmu_llc_miss_rate"} <= metrics.keys()
    assert all(row["mae"] == 0 for row in metrics.values())
    assert report["test_updates"] == 0


def test_bfcl_preserves_native_state_and_turns(monkeypatch, tmp_path):
    import sys
    from types import ModuleType, SimpleNamespace
    from benchmarks.backends import _BFCLImplementation as BFCLBackend
    monkeypatch.setattr(sys, "pycache_prefix", sys.pycache_prefix)
    monkeypatch.setenv("BFCL_PROJECT_ROOT", str(tmp_path))
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
    monkeypatch.setattr("benchmarks.compose.compose_argv", lambda **_kwargs: ["docker", "compose"])
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


@pytest.mark.parametrize("parallelism", [2, 8])
def test_runner_runs_concurrently_and_flushes_async_kb_once(monkeypatch, tmp_path, parallelism):
    import threading
    from types import SimpleNamespace
    from benchmarks import runner, runtime
    from swe_rebench import config, prepare, host_openclaw, runner as old_runner
    seed = tmp_path / "seed"
    make_seed(seed)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm: {}\n")
    cfg = SimpleNamespace(llm=SimpleNamespace(api_key="test", model="test"),
        runtime=SimpleNamespace(), batch=SimpleNamespace(parallelism=1, task_timeout_seconds=1200), output=SimpleNamespace())
    monkeypatch.setattr(config.RunnerConfig, "from_yaml", lambda *a, **k: cfg)
    monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prepare, "build_runtime_assets", lambda cfg: tmp_path)
    starts, stops, observed, pending, barriers = [], [], [], [], []
    active = 0
    max_active = 0
    lock = threading.Lock()
    overlap = threading.Barrier(parallelism)
    monkeypatch.setattr(host_openclaw, "_start_sidecar", lambda **k: starts.append(k) or "process")
    monkeypatch.setattr(host_openclaw, "_stop_process", lambda p: stops.append(p))
    def execute(task, cfg, assets, folder, port):
        nonlocal active, max_active
        path = folder / "kb"
        with lock:
            active += 1
            max_active = max(max_active, active)
            observed.append(json.loads((path / "state.json").read_text())["generation"])
            pending.append(task.task_id)
        overlap.wait(timeout=2)
        with lock:
            active -= 1
        return SimpleNamespace(task_id=task.task_id, exit_code=0)
    def flush_all_kb_updates(port, runtime_ids, *, gateway_id="swe-rebench"):
        assert len(set(runtime_ids)) == 2 * parallelism
        path = tmp_path / "run/kb"
        assert set(pending) == {str(i) for i in range(2 * parallelism)}
        with StateStore(path) as store:
            kb = RuntimeToolResourceKB()
            for index, task_id in enumerate(sorted(pending), 1):
                kb.observe_completed_call(CompletedCall("repo", f"read-{task_id}", None, 0, index))
            write_json(path / FILES[1], kb.to_json_obj())
            store.checkpoint()
        barriers.append(json.loads((path / "state.json").read_text())["generation"])
    monkeypatch.setattr(runtime, "execute", execute)
    monkeypatch.setattr(runtime, "flush_all_kb_updates", flush_all_kb_updates)
    monkeypatch.setattr(old_runner, "_result_dict", lambda result: {"task_id": result.task_id,
        "exit_code": result.exit_code, "error": None, "resource_summary": {"tool_span_ends": 1}})
    tasks = [ADAPTERS["deep-research-bench"]({"id": i, "prompt": "work"}) for i in range(2 * parallelism)]
    result = runner.run(tasks, config_path=cfg_path, seed=seed,
                        output=tmp_path / "run", parallelism=parallelism)
    assert result["status"] == "completed" and observed == [1] * (2 * parallelism)
    assert max_active == parallelism
    assert barriers == [2]
    assert result["parallelism"] == parallelism
    assert result["kb_flush_complete"] is True
    assert result["kb_final_generation"] == 2
    assert len(starts) == 1 and stops == ["process"]
    assert all(row["learning_status"] == "no_shared_kb_commit_observed_during_task"
               for row in result["results"])
    runner.run(tasks, config_path=cfg_path, seed=seed, resume=tmp_path / "run")
    assert len(starts) == 1
    result["active_tasks"] = [tasks[0].task_id]
    write_json(tmp_path / "run/run.json", result)
    with pytest.raises(ValueError, match="partial learning"):
        runner.run(tasks, config_path=cfg_path, seed=seed, resume=tmp_path / "run")


@pytest.mark.parametrize("workers", [2, 8])
def test_concurrent_runtime_uses_task_local_config(monkeypatch, tmp_path, workers):
    import concurrent.futures
    import threading
    from types import SimpleNamespace
    from benchmarks import runtime
    from swe_rebench import host_openclaw
    from swe_rebench.docker import ContainerResult

    tasks = [
        ADAPTERS["swe-rebench"]({
            "instance_id": f"org__repo-{index}",
            "repo": f"org/repo-{index}",
            "problem_statement": "fix",
            "docker_image": f"image-{index}",
        })
        for index in range(workers)
    ]
    shared = SimpleNamespace()
    overlap = threading.Barrier(workers)
    seen = []

    def run_host_openclaw_task(**kwargs):
        config = kwargs["config"]
        seen.append((id(config), config.kb_repo, config.task_directory,
                     config.flush_kb_on_task_drain))
        overlap.wait(timeout=2)
        return ContainerResult(task_id=kwargs["task"].instance_id,
                               image=kwargs["task"].image, exit_code=0)

    monkeypatch.setattr(host_openclaw, "run_host_openclaw_task", run_host_openclaw_task)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(
            lambda task: runtime.execute(task, shared, tmp_path, tmp_path / "run", 8765),
            tasks,
        ))

    assert [result.task_id for result in results] == [task.task_id for task in tasks]
    assert len({row[0] for row in seen}) == workers
    assert {(row[1], row[2]) for row in seen} == {
        (f"swe-rebench:org/repo-{index}", tasks[index].directory_name)
        for index in range(workers)
    }
    assert all(row[3] is False for row in seen)
    assert not hasattr(shared, "kb_repo")


@pytest.mark.parametrize("failure", ["interrupt", "executor", "barrier"])
def test_runner_failure_cancels_workers_and_preserves_report(monkeypatch, tmp_path, failure):
    import threading
    import time
    from types import SimpleNamespace
    from benchmarks import runner, runtime
    from swe_rebench import config, prepare, host_openclaw, runner as old_runner
    from swe_rebench.cancellation import check_cancelled

    seed = tmp_path / "seed"
    make_seed(seed)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm: {}\n")
    cfg = SimpleNamespace(llm=SimpleNamespace(api_key="test", model="test"),
        runtime=SimpleNamespace(), batch=SimpleNamespace(parallelism=2, task_timeout_seconds=1200), output=SimpleNamespace())
    monkeypatch.setattr(config.RunnerConfig, "from_yaml", lambda *a, **k: cfg)
    monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prepare, "build_runtime_assets", lambda cfg: tmp_path)
    monkeypatch.setattr(host_openclaw, "_start_sidecar", lambda **k: "process")
    monkeypatch.setattr(old_runner, "_result_dict", lambda result: {
        "task_id": result.task_id, "exit_code": result.exit_code,
        "error": result.error, "resource_summary": {"tool_span_ends": 1}})
    entered = threading.Barrier(3 if failure == "interrupt" else 2)
    cleaned = []
    starts = []
    stopped = []
    barriers = []

    def execute(task, *args):
        starts.append(task.task_id)
        try:
            if failure != "barrier":
                entered.wait(timeout=3)
                if failure == "executor" and task.task_id == "0":
                    raise RuntimeError("runtime cleanup failed")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    check_cancelled()
                    time.sleep(0.01)
                pytest.fail("worker did not receive cancellation")
            return SimpleNamespace(task_id=task.task_id, exit_code=0, error=None)
        finally:
            cleaned.append(task.task_id)

    def stop(process):
        assert set(cleaned) == set(starts)
        stopped.append(process)

    def flush(port, runtime_ids, *, gateway_id="swe-rebench"):
        barriers.append(runtime_ids)
        raise RuntimeError("real runtime did not drain")

    monkeypatch.setattr(runtime, "execute", execute)
    monkeypatch.setattr(runtime, "flush_all_kb_updates", flush)
    monkeypatch.setattr(host_openclaw, "_stop_process", stop)
    if failure == "interrupt":
        wait = runner.concurrent.futures.wait
        interrupted = False
        def interrupt_once(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                entered.wait(timeout=3)
                raise KeyboardInterrupt()
            return wait(*args, **kwargs)
        monkeypatch.setattr(runner.concurrent.futures, "wait", interrupt_once)

    tasks = [ADAPTERS["deep-research-bench"]({"id": i, "prompt": "work"}) for i in range(3)]
    if failure == "barrier":
        runner.run(tasks, config_path=cfg_path, seed=seed, output=tmp_path / "run")
    else:
        with pytest.raises(KeyboardInterrupt if failure == "interrupt" else RuntimeError):
            runner.run(tasks, config_path=cfg_path, seed=seed, output=tmp_path / "run")
    manifest = json.loads((tmp_path / "run/run.json").read_text())
    assert manifest == json.loads((tmp_path / "run/report.json").read_text())
    assert manifest["status"] == {"interrupt": "interrupted", "executor": "failed", "barrier": "failed"}[failure]
    assert manifest["kb_flush_complete"] is False
    assert "kb_final_generation" not in manifest
    assert {row["task_id"] for row in manifest["results"]} == set(starts)
    assert len(manifest["results"]) == len(starts)
    assert stopped == ["process"]
    if failure != "barrier":
        assert set(starts) == {"0", "1"}
        assert manifest["active_tasks"]
        assert len(barriers) == 1
    else:
        assert len(barriers) == 1
        assert manifest["observation_issues"]
    with pytest.raises(ValueError, match="partial learning|durability barrier"):
        runner.run(tasks, config_path=cfg_path, seed=seed, resume=tmp_path / "run")


@pytest.mark.parametrize("busy", [False, True])
def test_final_barrier_drains_every_real_runtime_before_flush(monkeypatch, busy):
    from benchmarks.runtime import flush_all_kb_updates
    from swe_rebench import host_openclaw
    drained = []
    def drain(port, runtime_id, **kwargs):
        drained.append((runtime_id, kwargs["flush_kb"]))
        if busy and runtime_id == "task-b":
            raise RuntimeError("active finalizer")
    monkeypatch.setattr(host_openclaw, "_drain_runtime", drain)
    if busy:
        with pytest.raises(RuntimeError, match="active finalizer"):
            flush_all_kb_updates(8765, ["task-a", "task-b"])
        assert drained == [("task-a", False), ("task-b", False)]
    else:
        flush_all_kb_updates(8765, ["task-a", "task-b"])
        assert drained == [("task-a", False), ("task-b", False), ("task-b", True)]


def test_concurrent_bridge_manifests_stay_process_local(monkeypatch, tmp_path):
    import concurrent.futures
    import os
    from types import SimpleNamespace
    from swe_rebench.host_openclaw import _openclaw_env

    monkeypatch.setenv("CLAWTUNE_BENCHMARK_TOOLS", "stale-global-manifest")

    def build(index):
        config = SimpleNamespace(
            docker=SimpleNamespace(cgroup_required=True, platform=""),
            benchmark_tools_manifest=str(tmp_path / f"manifest-{index}.json"),
        )
        return _openclaw_env(
            tmp_path / f"home-{index}", 8765 + index, config,
            tmp_path / f"workspace-{index}",
        )["CLAWTUNE_BENCHMARK_TOOLS"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(build, range(2)))

    assert values == [
        str(tmp_path / "manifest-0.json"),
        str(tmp_path / "manifest-1.json"),
    ]
    assert os.environ["CLAWTUNE_BENCHMARK_TOOLS"] == "stale-global-manifest"


def test_terminal_build_failures_do_not_abort_the_batch(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from benchmarks import runner, runtime, backends
    from benchmarks.adapters import Task
    from swe_rebench import config, prepare, host_openclaw
    seed = tmp_path / "seed"
    make_seed(seed)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm: {}\n")
    cfg = SimpleNamespace(llm=SimpleNamespace(api_key="test", model="test"),
        runtime=SimpleNamespace(), batch=SimpleNamespace(parallelism=1, task_timeout_seconds=0),
        docker=SimpleNamespace(platform="linux/amd64"), output=SimpleNamespace())
    monkeypatch.setattr(config.RunnerConfig, "from_yaml", lambda *a, **k: cfg)
    monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(prepare, "build_runtime_assets", lambda cfg: tmp_path)
    monkeypatch.setattr(host_openclaw, "_start_sidecar", lambda **k: "process")
    monkeypatch.setattr(host_openclaw, "_stop_process", lambda p: None)
    monkeypatch.setattr(runtime, "_required_terminal_preflight", lambda *a: None)
    started, barriers = [], []
    def build(task, *args, **kwargs):
        started.append(task.task_id)
        raise backends.TerminalCaseBuildFailure("compose up failed; cleanup succeeded")
    monkeypatch.setattr(backends, "TerminalBackend", build)
    monkeypatch.setattr(runtime, "flush_all_kb_updates", lambda port, ids, **_: barriers.append(ids))
    tasks = [Task("terminal-bench", name, "system", "terminal", "work",
                  payload={"task_path": str(tmp_path / "inputs" / name)})
             for name in ("first", "second")]
    result = runner.run(tasks, config_path=cfg_path, seed=seed, output=tmp_path / "run")
    assert started == ["first", "second"]
    assert len(result["results"]) == 2
    assert all(row["exit_code"] == 1 and "compose up failed" in row["error"] for row in result["results"])
    assert result["kb_flush_complete"] and len(barriers) == 1
