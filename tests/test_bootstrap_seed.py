import json
from pathlib import Path
import shlex

import pytest

from benchmarks.bootstrap import ROOT
from clawtune_kb import FILES, initialize_state, validate_seed
from clawtune_kb.store import digest
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from cold_start.flat_loader import read_task
from scripts.build_bootstrap_seed import build, replay
from tool_resource.runtime_kb import (
    ClauseResourceKB, RuntimeToolResourceKB, ToolCallQuery, PIPELINE_DEPENDENT_CONSUMER_BINS,
)
from tool_time.lattice_kb import LatticeTimeKB

SEED = ROOT / "seeds/bootstrap-v1"
ALL_FILES = (*FILES, "edge-kappa-kb.json")


@pytest.fixture
def simple_parser(monkeypatch):
    # Synthetic commands in these unit tests are simple argv, not shell programs.
    def parse(command):
        argv = shlex.split(command)
        return {"parse_failed": False, "clauses": [
            {"bin": argv[0], "argv": argv, "in_loop": False, "in_subst": False,
             "in_pipe": False, "pipeline_position": -1}]}
    monkeypatch.setattr("clawtune_sidecar.predictors.call_load.parse_command_clauses", parse)


def write_trace(path, task, starts, *, clauses=True, cpu=False):
    records = [{"type": "trace_metadata", "trace_format_version": 5,
                "instance_id": task, "repo": "owner/repo", "benchmark": "swe-rebench"}]
    for i, start in enumerate(starts):
        command = "python job.py --size " + str(i + 1)
        clause = {"bin": "python", "argv": shlex.split(command),
                  "ts_start": start + .1, "ts_end": start + .2, "latency_ms": 100.,
                  "eligible_for_kb": True, "telemetry_quality": "ok",
                  "in_loop": False, "in_subst": False, "in_pipe": False, "pipeline_position": -1,
                  "cpu_ns_cumulative": 999999, "peak_cpu_cores": 99.,
                  "availability": {"latency": "ok", "memory": "unknown", "cpu": "unknown"}}
        if cpu:
            clause["availability"]["cpu_time"] = "ok"
            clause["provenance"] = {"cpu_time_ns": 0}
        data = {"tool_name": "exec", "tool_call_id": str(i), "tool_args": {"command": command},
                "success": True, "duration_ms": 1000.,
                "resource_timeline": {"summary": {"cpu_core_s": 99.}}}
        if clauses:
            data["resource_observation"] = {"tool_call_id": str(i), "command": command,
                "eligible_for_kb": True, "telemetry_quality": "ok", "telemetry_status": "ok",
                "clauses": [clause]}
        records.append({"type": "action", "action_type": "tool_exec", "action_id": str(i),
                        "instance_id": task, "ts_start": start, "ts_end": start + 1., "data": data})
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_replay_selects_one_eligible_attempt_per_case_and_is_deterministic(tmp_path, simple_parser):
    dataset = tmp_path / "traces"
    dataset.mkdir()
    write_trace(dataset / "early-missing.jsonl", "owner__repo-1", [1.], clauses=False)
    write_trace(dataset / "first.jsonl", "owner__repo-2", [10., 12.])
    write_trace(dataset / "same-case-later.jsonl", "owner__repo-2", [15.])
    write_trace(dataset / "second.jsonl", "owner__repo-1", [20.])
    write_trace(dataset / "not-selected.jsonl", "owner__repo-3", [30.])
    before = {p.name: digest(p) for p in dataset.iterdir()}
    outputs = [tmp_path / "one", tmp_path / "two"]
    for output in outputs:
        result = build(output, dataset, cases=2, progress=lambda *a, **k: None)
        provenance = result["provenance"]
        assert [c["task_id"] for c in provenance["cases"]] == ["owner__repo-2", "owner__repo-1"]
        assert all(len(c["files"]) == 1 for c in provenance["cases"])
        assert provenance["counts"]["prediction_calls"] == 3
        assert provenance["target_counts"]["tool"]["duration_ms"] == 3
        assert provenance["target_counts"]["edge_kappa"]["duration_ms"] == 3
        for counts in provenance["target_counts"].values():
            assert counts["cpu_time_seconds"] == counts["memory_total_peak_bytes"] == 0
        edge = EdgeKappaRuntime.from_snapshot(json.loads((output / "edge-kappa-kb.json").read_text()))
        assert len(edge.to_snapshot()["runtime"]["completed_calls"]) == 3
        assert not edge.to_snapshot()["tokens"]
        assert len(edge.to_snapshot()["observations"]) == 3
        lattice = json.loads((output / FILES[2]).read_text())
        assert [r["ts_start"] for r in lattice["observations"]] == [10.1, 12.1, 20.1]
        assert all(r["cpu_ns_cumulative"] is None for r in lattice["observations"])
        assert lattice["pending"] == []
        for name in FILES[:2]:
            snapshot = json.loads((output / name).read_text())
            assert snapshot["repo"] and not any(snapshot["public"].values())
    for name in (*ALL_FILES, "manifest.json"):
        assert (outputs[0] / name).read_bytes() == (outputs[1] / name).read_bytes()
    assert {p.name: digest(p) for p in dataset.iterdir()} == before


def test_recorded_clocks_preserve_distinct_equal_duration_observations_and_zero_cpu(tmp_path):
    source = tmp_path / "case.jsonl"
    write_trace(source, "owner__repo-1", [10., 20.], cpu=True)
    loaded = read_task(source, repo="owner/repo", task_id="owner__repo-1", rss_unit="MiB",
                       preserve_timestamps=True)
    assert [c.ts_start for c in loaded.calls] == [10., 20.]
    assert [c.ts_start for c in loaded.clauses] == [10.1, 20.1]
    assert all(c.cpu_ns_cumulative == 0 for c in loaded.clauses)
    assert all(c.cpu_peak_cores is None and c.sampled_peak_rss_mb is None for c in loaded.clauses)
    assert all(not c.cpu_time_eligible for c in loaded.calls)
    data = [json.loads(line) for line in source.read_text().splitlines()]
    del data[1]["data"]["resource_observation"]["clauses"][0]["ts_start"]
    source.write_text("".join(json.dumps(r) + "\n" for r in data))
    loaded = read_task(source, repo="owner/repo", task_id="owner__repo-1", rss_unit="MiB",
                       preserve_timestamps=True)
    assert len(loaded.clauses) == 1
    assert loaded.counts["withheld_clause_without_contained_clock"] == 1


def _write_consumer_trace(path, consumer, position):
    write_trace(path, "owner__repo-1", [10.])
    records = [json.loads(line) for line in path.read_text().splitlines()]
    data = records[1]["data"]
    observation = data["resource_observation"]
    argv = [consumer, "-n", "1,20p"] if consumer == "sed" else [consumer]
    command = shlex.join(argv)
    if position > 0:
        command = "printf hello | " + command
    elif position == 0:
        command += " | cat"
    data["tool_args"]["command"] = observation["command"] = command
    observation["clauses"][0].update(
        bin=consumer, argv=argv, in_pipe=position >= 0, pipeline_position=position,
    )
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return observation


@pytest.mark.parametrize("consumer", sorted(PIPELINE_DEPENDENT_CONSUMER_BINS))
@pytest.mark.parametrize("position", [-1, 0, 1])
def test_tool_workload_uses_same_consumer_policy_online_and_offline(tmp_path, consumer, position):
    from clawtune_sidecar.predictors.tool_resource import _retained_workload_duration_seconds
    from tool_resource.runtime_kb import _target_values

    path = tmp_path / "case.jsonl"
    observation = _write_consumer_trace(path, consumer, position)
    loaded = read_task(path, repo="owner/repo", task_id="owner__repo-1", rss_unit="MiB",
                       preserve_timestamps=True)
    call = loaded.calls[0]
    targets = _target_values(call)
    online = _retained_workload_duration_seconds({"calls": [observation]})
    # The complete call remains stored, including its original elapsed metric.
    assert targets["latency_ms"] == 1000.
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(call)
    evidence = kb.predict_load_samples(ToolCallQuery("owner/repo", "exec", call.command, 12.))
    if position > 0:
        assert online is None
        assert call.workload_duration_seconds is None
        assert "workload_latency_ms" not in targets
        assert "duration_ms" not in loaded.call_actuals[0]
        assert not evidence.get("duration_ms", {}).get("values")
    else:
        assert online == pytest.approx(.1)
        assert targets["workload_latency_ms"] == pytest.approx(100.)
        assert loaded.call_actuals[0]["duration_ms"] == pytest.approx(100.)
        assert evidence["duration_ms"]["values"] == pytest.approx((100.,))


@pytest.mark.parametrize("clock", ["record-order", "trace"])
@pytest.mark.parametrize("parser_available", [False, True])
@pytest.mark.parametrize("position", [-1, 0, 1])
def test_edge_sed_admission_is_independent_of_clock_and_parser(
    tmp_path, monkeypatch, clock, parser_available, position,
):
    from offline.edge_kappa_eval import _events
    from tool_resource.mvdan_client import MvdanClientError

    path = tmp_path / "case.jsonl"
    _write_consumer_trace(path, "sed", position)
    if not parser_available:
        def unavailable(command):
            raise MvdanClientError("parser unavailable")
        monkeypatch.setattr("tool_resource.features.parse_command_clauses", unavailable)
    task = dict(benchmark="swe-rebench", group="owner/repo", task_id="owner__repo-1",
                files=[dict(path=path.name, version=5, sha256=digest(path))])
    events, _ = _events(tmp_path, {"case": task}, ["case"], "MiB", clock)
    assert len(events) == int(position <= 0)


@pytest.mark.parametrize("compound", [False, True])
@pytest.mark.parametrize("clock", ["missing", "before", "after", "reversed", "valid"])
@pytest.mark.parametrize("preserve_timestamps", [False, True])
def test_tool_workload_requires_all_clause_clocks(tmp_path, compound, clock, preserve_timestamps):
    from tool_resource.runtime_kb import _target_values

    source = tmp_path / "case.jsonl"
    write_trace(source, "owner__repo-1", [10.])
    records = [json.loads(line) for line in source.read_text().splitlines()]
    data = records[1]["data"]
    observation = data["resource_observation"]
    clauses = observation["clauses"]
    if compound:
        # Overlapping intervals must be unioned, not summed (150 ms, not 200 ms).
        clauses.append(dict(clauses[0], argv=["python", "other.py"],
                            ts_start=10.15, ts_end=10.25))
        command = data["tool_args"]["command"] + " & python other.py"
        data["tool_args"]["command"] = observation["command"] = command
    clause = clauses[-1]
    if clock == "missing":
        del clause["ts_start"]
    elif clock == "before":
        clause.update(ts_start=9., ts_end=9.1)
    elif clock == "after":
        clause.update(ts_start=100., ts_end=200., latency_ms=100000.)
    elif clock == "reversed":
        clause.update(ts_start=10.3, ts_end=10.2)
    source.write_text("".join(json.dumps(row) + "\n" for row in records))

    loaded = read_task(source, repo="owner/repo", task_id="owner__repo-1",
                       rss_unit="MiB", preserve_timestamps=preserve_timestamps)
    call = loaded.calls[0]
    targets = _target_values(call)
    if clock == "valid":
        expected_ms = 150. if compound else 100.
        assert targets["workload_latency_ms"] == pytest.approx(expected_ms)
        assert loaded.call_actuals[0]["duration_ms"] == pytest.approx(expected_ms)
        assert len(loaded.clauses) == len(clauses)
    else:
        assert loaded.counts["withheld_clause_without_contained_clock"] == 1
        assert len(loaded.clauses) == int(compound)
        assert call.workload_duration_seconds is None
        assert "workload_latency_ms" not in targets
        assert "duration_ms" not in loaded.call_actuals[0]
        # Preserve the independently recorded tool interval, but do not expose
        # it as the workload duration when clause telemetry was rejected.
        assert targets["latency_ms"] == 1000.
        kb = RuntimeToolResourceKB()
        kb.observe_completed_call(call)
        evidence = kb.predict_load_samples(ToolCallQuery("owner/repo", "exec", call.command, 12.))
        assert not evidence.get("duration_ms", {}).get("values")


def test_replay_prediction_precedes_completion_and_no_future_labels(tmp_path, simple_parser, monkeypatch):
    source = tmp_path / "case.jsonl"
    write_trace(source, "owner__repo-1", [10., 10.5, 13.])
    loaded = read_task(source, repo="owner/repo", task_id="owner__repo-1", rss_unit="MiB",
                       preserve_timestamps=True)
    import scripts.build_bootstrap_seed as builder
    actual = builder.predict_call_load
    observed = []
    def capture(**kwargs):
        result = actual(**kwargs)
        snapshot = kwargs["edge_kappa"].to_snapshot()
        observed.append(len(snapshot["observations"]))
        return result
    monkeypatch.setattr(builder, "predict_call_load", capture)
    payloads, _, _ = replay([("case", {"group": "owner/repo"}, 10.,
                             [({"path": source.name}, loaded)])], progress=lambda *a, **k: None)
    assert observed == [0, 0, 2]
    assert len(payloads["edge-kappa-kb.json"]["observations"]) == 3


def test_recipe_rejects_insufficient_cases_without_creating_bundle(tmp_path, simple_parser):
    dataset = tmp_path / "data"
    dataset.mkdir()
    write_trace(dataset / "case.jsonl", "owner__repo-1", [1.], clauses=False)
    with pytest.raises(ValueError, match="need 1 cases"):
        build(tmp_path / "seed", dataset, cases=1)
    assert not (tmp_path / "seed").exists()


def test_shipped_seed_contains_four_finalized_kbs_and_ten_cases():
    manifest = validate_seed(SEED)
    assert set(manifest["snapshots"]) == set(ALL_FILES)
    provenance = manifest["provenance"]
    assert provenance["case_count"] == len(provenance["cases"]) == 10
    assert len({c["task_id"] for c in provenance["cases"]}) == 10
    assert all(len(c["files"]) == 1 for c in provenance["cases"])
    for name in ALL_FILES:
        snapshot = json.loads((SEED / name).read_text(encoding="utf-8"))
        assert snapshot["pending"] == []
    for backend in ("tool", "trie", "lattice", "edge_kappa"):
        assert provenance["target_counts"][backend]["duration_ms"] > 0
        assert "memory_total_peak_bytes" in provenance["unavailable_targets"][backend]
    edge = EdgeKappaRuntime.from_snapshot(json.loads((SEED / ALL_FILES[3]).read_text()), frozen=True)
    assert not edge.to_snapshot()["tokens"]
    assert edge.to_snapshot()["runtime"]["completed_calls"]


def test_initial_state_can_learn_without_mutating_release_seed(tmp_path):
    before = {name: digest(SEED / name) for name in (*ALL_FILES, "manifest.json")}
    state = tmp_path / "kb"
    initialize_state(state, SEED, owner="daily")
    assert set(validate_seed(SEED)["snapshots"]) <= {p.name for p in state.iterdir()}
    trie = ClauseResourceKB.from_json_obj(json.loads((state / FILES[0]).read_text()))
    from tool_resource.runtime_kb import ClauseObservation
    trie.observe_completed_clause(ClauseObservation(
        repo="new-project", bin="find", argv=("find", "."), ts_start=1, ts_end=2, latency_ms=1000))
    trie.predict_load_samples("new-project", [{"bin": "find", "argv": ["find", "."]}], 3)
    assert "new-project" in trie.to_json_obj()["repo"]
    assert {name: digest(SEED / name) for name in before} == before


def test_cli_default_uses_release_seed():
    from benchmarks.cli import parser
    assert parser().parse_args(["benchmark"]).seed == SEED


def test_unrecorded_resources_remain_unavailable_in_shipped_seed():
    manifest = validate_seed(SEED)
    repo = manifest["provenance"]["cases"][0]["repo"]
    tool = RuntimeToolResourceKB.from_json_obj(json.loads((SEED / FILES[1]).read_text()))
    tool.freeze()
    evidence = tool.predict_load_samples(ToolCallQuery(repo, "exec", "python job.py", 1))
    for target in ("cpu_time_seconds", "cpu_peak_cores", "memory_total_peak_bytes", "memory_extra_peak_bytes"):
        assert not evidence.get(target, {}).get("values")
    assert tool.predict_pmu_samples(ToolCallQuery(repo, "exec", "python job.py", 1)) == {}
