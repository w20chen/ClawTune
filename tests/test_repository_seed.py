import json

import pytest

from offline.runner import inventory, load_or_create_split
from scripts.build_replayed_seed import build_repository_split
from tool_resource.runtime_kb import ClauseResourceKB


def source(task, repo, argv):
    command = " ".join(argv)
    clause = {"bin": argv[0], "argv": argv,
              "ts_start": 1., "ts_end": 2., "latency_ms": 1000.,
              "status": {"state": "exited"}, "availability": {"latency": "ok"},
              "in_loop": False, "in_pipe": False, "in_subst": False, "pipeline_position": -1}
    return [
        {"record_type": "trace_metadata", "schema_version": 6, "trace_format_version": 6,
         "benchmark": "swe-rebench", "instance_id": task, "repo": repo},
        {"record_type": "span_start", "kind": "tool", "span_id": "call", "name": "exec",
         "repo": repo, "wall_time_ns": "1000000000",
         "input": {"requested_args": {"command": command}}},
        {"record_type": "span_end", "kind": "tool", "span_id": "call", "name": "exec",
         "repo": repo, "wall_time_ns": "2000000000", "duration_ns": "1000000000",
         "status": {"code": "ok"}, "execution": {"execution_id": "call",
         "tool_resource": {"call_telemetry": {"command": command, "clauses": [clause],
             "eligible_for_kb": True, "telemetry_quality": "ok"}}}},
    ]


def test_bundled_seed_does_not_repeat_measured_clause_as_zero_labels():
    from benchmarks.bootstrap import ROOT

    seed = ROOT / "seeds/bootstrap-v2"
    repo, argv = "briancurtin/deprecation", ["python3", "-c", "import unittest2"]
    lattice = json.loads((seed / "clause-lattice-time-kb.json").read_text(encoding="utf-8"))
    rows = [row for row in lattice["observations"] if row["repo"] == repo and row["argv"] == argv]
    assert len(rows) == 1
    trie = ClauseResourceKB.from_json_obj(json.loads((seed / "clause-resource-kb.json").read_text(encoding="utf-8")))
    samples = trie.predict_load_samples(repo, [{"bin": "python3", "argv": argv}], 10)[0]
    for target, expected in {"cpu_time_seconds": .02793228, "sampled_peak_rss_bytes": 10203136.,
                             "memory_total_peak_bytes": 20054016., "memory_extra_peak_bytes": 4304896.}.items():
        assert tuple(samples[target]["values"]) == (expected,)


@pytest.mark.parametrize("argv", [["ls", "-la"], ["python", "work.py"]])
def test_repository_seed_preserves_runtime_names_and_excludes_test_data(tmp_path, argv):
    dataset = tmp_path / "input"
    dataset.mkdir()
    for name in ("alpha", "beta"):
        (dataset / f"{name}.trace.jsonl").write_text(
            "\n".join(map(json.dumps, source(f"org__{name}-1", f"org/{name}", argv))), encoding="utf-8")
    tasks = inventory(dataset, "swe-rebench")
    split = load_or_create_split(tasks, dataset, 42, tmp_path / "splits", .5, "repository")
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps(split), encoding="utf-8")
    train_repo = tasks[split["train"][0]]["group"]
    test_repo = tasks[split["test"][0]]["group"]
    # A test trace need not even be readable to build the training seed.
    (dataset / tasks[split["test"][0]]["files"][0]["path"]).write_text("not training input")
    output = tmp_path / "seed"
    manifest = build_repository_split(dataset, split_path, output)
    provenance = manifest["provenance"]
    assert provenance["train_repositories"] == [train_repo]
    assert provenance["test_repositories"] == [test_repo]
    assert [row["task_id"] for row in provenance["sources"]] == [tasks[split["train"][0]]["task_id"]]
    assert provenance["test_updates"] == 0
    payload = json.loads((output / "clause-resource-kb.json").read_text())
    assert set(payload["repo"]) == {train_repo}
    kb = ClauseResourceKB.from_json_obj(payload)
    for repo in (train_repo, test_repo):
        values = kb.predict_load_samples(repo, [{"bin": argv[0], "argv": argv}], 10)
        assert set(values[0]["duration_ms"]["values"]) == {1000.}
        if repo == test_repo:
            assert values[0]["duration_ms"]["context"][0] == "public"
            if argv == ["python", "work.py"]:
                # The global prior is allowed without borrowing repo script identity.
                assert values[0]["duration_ms"]["context"][1] == "global"
    split["tasks"][split["test"][0]]["group"] = train_repo
    split_path.write_text(json.dumps(split), encoding="utf-8")
    with pytest.raises(ValueError, match="test repositories"):
        build_repository_split(dataset, split_path, tmp_path / "bad-seed")


def test_deployment_seed_can_include_original_test_repositories(tmp_path):
    from clawtune_kb.store import digest
    from tool_resource.runtime_kb import RuntimeToolResourceKB, ToolCallQuery

    dataset = tmp_path / "input"
    dataset.mkdir()
    for name in ("alpha", "beta"):
        (dataset / f"{name}.trace.jsonl").write_text(
            "\n".join(map(json.dumps, source(f"org__{name}-1", f"org/{name}", ["sleep", "1"]))),
            encoding="utf-8")
    tasks = inventory(dataset, "swe-rebench")
    split = load_or_create_split(tasks, dataset, 42, tmp_path / "splits", .5, "repository")
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps(split), encoding="utf-8")
    before = {p.name: digest(p) for p in dataset.iterdir()}
    output = tmp_path / "seed"
    p = build_repository_split(dataset, split_path, output, include_test=True)["provenance"]
    assert p["kind"] == "repository-all-trace-v6"
    assert p["counts"]["tasks"] == p["counts"]["calls"] == 2
    assert p["included_original_test_tasks"] == 1
    assert p["test_repositories"] == [] and "test_updates" not in p
    assert set(p["train_repositories"]) == {"org/alpha", "org/beta"}
    tool = RuntimeToolResourceKB.from_json_obj(json.loads((output / "runtime-tool-resource-kb.json").read_text()))
    for repo in p["train_repositories"]:
        samples = tool.predict_load_samples(ToolCallQuery(repo, "exec", "sleep 1", 10))
        assert samples["duration_ms"]["context"] == ("repo", "exact_command")
        assert samples["duration_ms"]["values"] == (1000.,)
    assert before == {p.name: digest(p) for p in dataset.iterdir()}


def split_resource_trace():
    from dataclasses import asdict
    from tool_resource.runtime_kb import ClauseObservation

    rows = source("org__repo-1", "org/repo", ["python", "-m", "pytest"])
    end = rows[-1]
    end.update(wall_time_ns=1_100_000_000, duration_ns=100_000_000)
    clause = end["execution"]["tool_resource"]["call_telemetry"]["clauses"][0]
    clause.update(ts_end=1.1, latency_ms=100., peak_cpu_cores=.8, sampled_peak_rss_mb=8.,
                  provenance={"cpu_time_ns": 80_000_000},
                  availability={"latency": "ok", "cpu_time": "ok", "cpu": "ok", "memory": "ok"})
    memory = ClauseObservation("org/repo", "python", ("python", "-m", "pytest"), 1., 1.1,
        memory_baseline_bytes=1000, memory_total_peak_bytes=2000, memory_extra_peak_bytes=1000,
        memory_measurement="cgroup_v2_environment_union_v1", memory_environment_id="env",
        memory_eligible=True, clause_index=0)
    end["resources"] = {"memory_clause_observations": [asdict(memory)]}
    return rows


@pytest.mark.parametrize("relaxed", [False, True])
def test_merge_complementary_clause_rows_before_recovering_labels(tmp_path, relaxed):
    from clawtune_kb.store import digest
    from clawtune_sidecar.predictors.tool_resource import COLD_START_V2_LABEL_POLICY, STRICT_TRACE_LABEL_POLICY
    from offline.runner import load_task
    from scripts.build_replayed_seed import _load

    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(map(json.dumps, split_resource_trace())), encoding="utf-8")
    policy = COLD_START_V2_LABEL_POLICY if relaxed else STRICT_TRACE_LABEL_POLICY
    task = {"benchmark": "swe-rebench", "group": "org/repo", "task_id": "org__repo-1",
            "files": [{"path": path.name, "version": 6, "sha256": digest(path)}]}
    for loaded in (load_task(tmp_path, task, "MB", recorded_clauses_only=True, trace_label_policy=policy),
                   _load("org__repo-1", path, label_policy=policy)[1]):
        assert len(loaded.clauses) == 1
        row = loaded.clauses[0]
        assert row.cpu_ns_cumulative == 80_000_000
        assert row.cpu_peak_cores == .8
        assert row.sampled_peak_rss_mb == 8.
        assert row.memory_total_peak_bytes == 2000
        assert row.memory_extra_peak_bytes == 1000
        assert row.memory_environment_id == "env"


def test_partial_resource_values_survive_unavailable_clause_telemetry(tmp_path):
    from clawtune_kb.store import digest
    from offline.runner import load_task
    from scripts.build_replayed_seed import _load

    rows = split_resource_trace()
    rows[-1]["execution"]["tool_resource"]["call_telemetry"]["eligible_for_kb"] = False
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    task = {"benchmark": "swe-rebench", "group": "org/repo", "task_id": "org__repo-1",
            "files": [{"path": path.name, "version": 6, "sha256": digest(path)}]}
    for loaded in (load_task(tmp_path, task, "MB"), _load("org__repo-1", path)[1]):
        assert len(loaded.clauses) == 1
        assert loaded.clauses[0].memory_total_peak_bytes == 2000
        assert loaded.clauses[0].cpu_ns_cumulative == 80_000_000


def test_memory_clause_extension_metadata_is_ignored_by_trace_importers(tmp_path):
    from copy import deepcopy
    from jsonschema import Draft202012Validator
    from benchmarks.bootstrap import ROOT
    from clawtune_kb.store import digest
    from clawtune_sidecar.predictors.tool_resource import load_openclaw_trace_observations
    from offline.runner import load_task
    from scripts.build_replayed_seed import _load

    rows = split_resource_trace()
    memory = rows[-1]["resources"]["memory_clause_observations"][0]
    memory["producer_metadata"] = {"sample_id": "extension-fixture"}
    # The public observation contract explicitly permits producer extensions.
    schema = json.loads((ROOT / "contracts/clawbox-seed-observations.schema.json").read_text(encoding="utf-8"))
    observation_schema = deepcopy(schema["$defs"]["clauseRecord"]["properties"]["observation"])
    observation_schema["$defs"] = schema["$defs"]
    Draft202012Validator(observation_schema).validate(json.loads(json.dumps(memory)))
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    task = {"benchmark": "swe-rebench", "group": "org/repo", "task_id": "org__repo-1",
            "files": [{"path": path.name, "version": 6, "sha256": digest(path)}]}
    loaded = load_openclaw_trace_observations(path, repo="org/repo")
    assert loaded.observations[0].memory_total_peak_bytes == 2000
    for loaded in (load_task(tmp_path, task, "MB"), _load("org__repo-1", path)[1]):
        assert len(loaded.clauses) == 1
        assert loaded.clauses[0].memory_total_peak_bytes == 2000
        assert loaded.clauses[0].cpu_ns_cumulative == 80_000_000


def test_identical_clause_clocks_in_different_calls_remain_separate(tmp_path):
    from copy import deepcopy
    from clawtune_kb.store import digest
    from clawtune_sidecar.predictors.tool_resource import COLD_START_V2_LABEL_POLICY
    from offline.runner import load_task

    rows = split_resource_trace()
    second = deepcopy(rows[1:])
    for row in second:
        row["span_id"] = "second-call"
    rows.extend(second)
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    task = {"benchmark": "swe-rebench", "group": "org/repo", "task_id": "org__repo-1",
            "files": [{"path": path.name, "version": 6, "sha256": digest(path)}]}
    loaded = load_task(tmp_path, task, "MB", trace_label_policy=COLD_START_V2_LABEL_POLICY)
    assert len(loaded.clauses) == len(loaded.calls) == 2
    assert [row.cpu_ns_cumulative for row in loaded.clauses] == [80_000_000, 80_000_000]


def test_merge_preserves_distinct_memory_measurements_without_sibling_zeros():
    from dataclasses import replace
    from clawtune_sidecar.predictors.tool_resource import COLD_START_V2_LABEL_POLICY
    from offline.runner import merge_trace_clause_observations
    from tool_resource.runtime_kb import ClauseObservation

    memory = ClauseObservation("org/repo", "python", ("python",), 1., 1.1,
        memory_baseline_bytes=1000, memory_total_peak_bytes=2000, memory_extra_peak_bytes=1000,
        memory_measurement="cgroup_v2_environment_union_v1", memory_environment_id="env", memory_eligible=True)
    guest = replace(memory, memory_measurement="guest_memtotal_minus_memavailable", memory_environment_id="vm")
    cpu = ClauseObservation("org/repo", "python", ("python",), 1., 1.1, cpu_ns_cumulative=80_000_000)
    rows = merge_trace_clause_observations([memory, guest, cpu], COLD_START_V2_LABEL_POLICY)
    assert len(rows) == 2
    assert {row.memory_measurement for row in rows} == {memory.memory_measurement, guest.memory_measurement}
    assert [row.cpu_ns_cumulative for row in rows if row.cpu_ns_cumulative is not None] == [80_000_000]
    assert all(row.memory_total_peak_bytes == 2000 for row in rows)


@pytest.mark.parametrize("legacy_policy", ["strict", "short-zero-shared-v1"])
def test_partial_long_clause_retains_values_and_imputes_only_missing_targets(legacy_policy):
    from clawtune_sidecar.predictors.tool_resource import COLD_START_V2_LABEL_POLICY, STRICT_TRACE_LABEL_POLICY
    from offline.runner import merge_trace_clause_observations, _static_clause
    from tool_resource.runtime_kb import ClauseObservation

    policy = STRICT_TRACE_LABEL_POLICY if legacy_policy == "strict" else COLD_START_V2_LABEL_POLICY
    cpu = ClauseObservation("org/repo", "python", ("python",), 20., 80.,
        latency_ms=60_000., cpu_ns_cumulative=4_000_000_000, censored=True, confirmed_timeout=True)
    memory = ClauseObservation("org/repo", "python", ("python",), 20., 80.,
        memory_baseline_bytes=1000, memory_total_peak_bytes=2000, memory_extra_peak_bytes=1000,
        memory_measurement="cgroup_v2_environment_union_v1", memory_environment_id="env",
        memory_eligible=False, censored=True, confirmed_timeout=True)
    rows = merge_trace_clause_observations([cpu, memory], policy)
    assert len(rows) == 1
    row = _static_clause(rows[0], "swe-rebench:org/repo")
    assert row.censored and row.confirmed_timeout
    assert row.ts_start == 0. and row.ts_end == 60.
    assert row.cpu_ns_cumulative == 4_000_000_000
    assert row.cpu_peak_cores == row.sampled_peak_rss_mb == 0.
    assert row.memory_total_peak_bytes == 2000 and row.memory_extra_peak_bytes == 1000
    assert set(row.imputed_targets) == {"cpu_peak_cores", "sampled_peak_rss_bytes"}
    from tool_time.lattice_kb import LatticeTimeKB
    for kb in (ClauseResourceKB.fit_public([row]), LatticeTimeKB.fit([row])):
        samples = kb.predict_load_samples(row.repo, [{"bin": "python", "argv": ["python"]}], 100.)[0]
        assert tuple(samples["cpu_time_seconds"]["values"]) == (4.,)
        assert tuple(samples["sampled_peak_rss_bytes"]["values"]) == (0.,)
        assert tuple(samples["memory_total_peak_bytes"]["values"]) == (2000.,)


def test_partial_trace_rows_are_preserved_by_both_seed_ingress_paths(tmp_path):
    from clawtune_kb.store import digest
    from offline.runner import load_task
    from scripts.build_replayed_seed import _load

    rows = split_resource_trace()
    end = rows[-1]
    end["status"] = {"code": "error", "message": "tool_timeout"}
    telemetry = end["execution"]["tool_resource"]
    telemetry.update(replay_execution="incomplete", confirmed_timeout=True,
                     timeout_duration_lower_bound_ms=100.)
    telemetry["call_telemetry"].update(eligible_for_kb=False, telemetry_quality="partial")
    clause = telemetry["call_telemetry"]["clauses"][0]
    clause["status"] = {"state": "signaled", "signal": 9}
    clause["availability"] = {"latency": "partial", "cpu_time": "partial", "cpu": "partial", "memory": "partial"}
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    task = {"benchmark": "swe-rebench", "group": "org/repo", "task_id": "org__repo-1",
            "files": [{"path": path.name, "version": 6, "sha256": digest(path)}]}
    for loaded in (load_task(tmp_path, task, "MB"), _load("org__repo-1", path)[1]):
        assert len(loaded.calls) == len(loaded.clauses) == 1
        assert loaded.calls[0].censored and loaded.calls[0].confirmed_timeout
        assert loaded.calls[0].timeout_duration_lower_bound_ms == 100.
        row = loaded.clauses[0]
        assert row.censored and row.confirmed_timeout
        assert row.cpu_ns_cumulative == 80_000_000
        assert row.cpu_peak_cores == .8 and row.sampled_peak_rss_mb == 8.
        assert row.memory_total_peak_bytes == 2000 and row.memory_extra_peak_bytes == 1000


def test_recorded_imputed_zero_yields_to_real_sibling_memory():
    from clawtune_sidecar.predictors.tool_resource import STRICT_TRACE_LABEL_POLICY
    from offline.runner import merge_trace_clause_observations
    from tool_resource.runtime_kb import ClauseObservation

    cpu = ClauseObservation("org/repo", "python", ("python",), 1., 2.,
        cpu_ns_cumulative=80_000_000, memory_total_peak_bytes=0, memory_extra_peak_bytes=0,
        memory_measurement="cgroup_v2_environment_union_v1",
        imputed_targets=("memory_total_peak_bytes", "memory_extra_peak_bytes"))
    memory = ClauseObservation("org/repo", "python", ("python",), 1., 2.,
        memory_baseline_bytes=1000, memory_total_peak_bytes=2000, memory_extra_peak_bytes=1000,
        memory_measurement="guest_memtotal_minus_memavailable", memory_environment_id="vm", memory_eligible=True)
    rows = merge_trace_clause_observations([cpu, memory], STRICT_TRACE_LABEL_POLICY)
    assert len(rows) == 1
    assert rows[0].cpu_ns_cumulative == 80_000_000
    assert rows[0].memory_total_peak_bytes == 2000 and rows[0].memory_extra_peak_bytes == 1000
    assert "memory_total_peak_bytes" not in rows[0].imputed_targets


def test_trace_cli_propagates_and_records_label_policy(tmp_path, monkeypatch, capsys):
    import scripts.build_replayed_seed as builder
    from clawtune_sidecar.predictors.tool_resource import COLD_START_V2_LABEL_POLICY

    (tmp_path / "input").mkdir()
    path = tmp_path / "input/trace.jsonl"
    rows = split_resource_trace()
    del rows[-1]["execution"]["tool_resource"]["call_telemetry"]["clauses"][0]["peak_cpu_cores"]
    path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    strict = builder._load("org__repo-1", path)[1]
    relaxed = builder._load("org__repo-1", path, label_policy=COLD_START_V2_LABEL_POLICY)[1]
    assert strict.clauses[0].cpu_peak_cores == 0.
    assert relaxed.clauses[0].cpu_peak_cores == 0.
    monkeypatch.setattr("clawtune_sidecar.predictors.call_load.parse_command_clauses",
        lambda command: {"parse_failed": False, "clauses": [{"bin": "python", "argv": ["python", "-m", "pytest"],
            "in_loop": False, "in_subst": False, "in_pipe": False, "pipeline_position": -1}]})
    monkeypatch.setattr(builder, "_prediction_validation", lambda *args: {"passed": True})
    monkeypatch.setattr("sys.argv", ["build_replayed_seed", "--trace", f"org__repo-1={path}",
        "--output", str(tmp_path / "seed"), "--trace-label-policy", COLD_START_V2_LABEL_POLICY.name])
    builder.main()
    manifest = json.loads((tmp_path / "seed/manifest.json").read_text(encoding="utf-8"))
    assert manifest["provenance"]["label_policy"]["name"] == COLD_START_V2_LABEL_POLICY.name
    assert manifest["provenance"]["label_policy"]["effective_training_policy"] == "available-or-zero-v1"
    assert manifest["provenance"]["label_policy"]["impute_short_zeros"] is True
    assert manifest["provenance"]["clause_target_counts"]["cpu_peak_cores"] == 1
    capsys.readouterr()


def test_partial_observations_stay_in_training_but_not_complete_task_holdout():
    from types import SimpleNamespace
    from tool_resource.runtime_kb import CompletedCall
    from scripts.build_replayed_seed import _holdout

    complete = CompletedCall("org/repo", "exec", "sleep 1", 0., 1.)
    partial = CompletedCall("org/repo", "exec", "sleep 9", 0., 2., censored=True,
                            confirmed_timeout=True, timeout_duration_lower_bound_ms=2000.)
    training, test = _holdout([("task", "org/repo", SimpleNamespace(calls=[complete, partial]))], .5)
    assert training == [partial]
    assert test == [("task", complete)]


def test_v5_bootstrap_replay_fills_missing_clause_targets_after_coalescing(tmp_path, monkeypatch):
    from test_bootstrap_seed import write_trace
    from cold_start.flat_loader import read_task
    from scripts.build_bootstrap_seed import replay
    from tool_time.lattice_kb import LatticeTimeKB

    path = tmp_path / "source.jsonl"
    write_trace(path, "owner__repo-1", [10.])
    loaded = read_task(path, repo="owner/repo", task_id="owner__repo-1", rss_unit="MiB", preserve_timestamps=True)
    monkeypatch.setattr("clawtune_sidecar.predictors.call_load.parse_command_clauses",
        lambda command: {"parse_failed": False, "clauses": [{"bin": "python", "argv": command.split(),
            "in_loop": False, "in_subst": False, "in_pipe": False, "pipeline_position": -1}]})
    payloads, _counts, targets = replay([("key", {"group": "owner/repo"}, 10.,
        [({"path": path.name}, loaded)])], progress=lambda *a, **k: None)
    for name in ("trie", "lattice"):
        assert targets[name]["cpu_time_seconds"] == targets[name]["memory_total_peak_bytes"] == 1
    lattice = payloads["clause-lattice-time-kb.json"]
    assert lattice["observations"][0]["cpu_ns_cumulative"] == 0
    assert lattice["observations"][0]["memory_total_peak_bytes"] == 0
    assert "cpu_time_seconds" in lattice["observations"][0]["imputed_targets"]
    LatticeTimeKB.from_json_obj(lattice)


def test_v5_offline_ingress_fills_missing_metrics_without_merging_calls(tmp_path):
    from test_bootstrap_seed import write_trace
    from clawtune_kb.store import digest
    from offline.runner import load_task

    path = tmp_path / "source.jsonl"
    write_trace(path, "owner__repo-1", [10., 20.])
    task = {"benchmark": "swe-rebench", "group": "owner/repo", "task_id": "owner__repo-1",
            "files": [{"path": path.name, "version": 5, "sha256": digest(path)}]}
    loaded = load_task(tmp_path, task, "MiB", recorded_clauses_only=True)
    assert len(loaded.calls) == len(loaded.clauses) == 2
    for row in loaded.clauses:
        assert row.cpu_ns_cumulative == row.memory_total_peak_bytes == row.sampled_peak_rss_mb == 0
        assert row.cpu_peak_cores == 99.
        assert "cpu_time_seconds" in row.imputed_targets
