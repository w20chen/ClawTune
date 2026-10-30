"""Latest ingestion policy retains partial/approximate values and marks imputation."""
from dataclasses import replace
import pytest
from tool_resource.runtime_kb import CompletedCall, ClauseObservation, _target_values, training_target_provenance, _clause_value
from clawtune_sidecar.predictors.tool_resource import completed_call_from_completion, apply_trace_label_policy_to_clause, STRICT_TRACE_LABEL_POLICY, load_openclaw_trace_observations
from clawtune_sidecar.trace import AgentTestBenchTraceWriter
from test_tool_resource_predictor import _runtime_sample, _tool_completion, _tool_request


def test_timeout_trains_effective_limit_for_primary_duration_and_preserves_prefix_resources():
    call = CompletedCall("r", "exec", "sleep 30", 100, 103, censored=True,
        confirmed_timeout=True, timeout_duration_lower_bound_ms=2500,
        workload_duration_seconds=2, workload_duration_required=True,
        cpu_time_seconds=.8, cpu_peak_cores=.7, sampled_peak_rss_bytes=4096,
        pmu_cycles=900, pmu_eligible=False)
    values = _target_values(call)
    assert values["latency_ms"] == values["workload_latency_ms"] == 2500
    assert values["cpu_time_seconds"] == .8 and values["cpu_avg_cores"] == .4
    assert values["cpu_peak_cores"] == .7 and values["sampled_peak_rss_bytes"] == 4096
    assert values["pmu_cycles"] == 900 and values["pmu_instructions"] == 0
    provenance = training_target_provenance(call)
    assert provenance["workload_latency_ms"] == "timeout_lower_bound"
    assert provenance["pmu_cycles"] == "observed_partial"
    assert provenance["pmu_instructions"] == "imputed_zero"


@pytest.mark.parametrize("censored", [True, False])
def test_missing_metrics_train_zero_without_overwriting_approximate_values(censored):
    call = CompletedCall("r", "read", None, 0, .01, censored=censored,
        cpu_avg_cores=.4, sampled_peak_rss_bytes=0)
    values = _target_values(call)
    assert values["cpu_avg_cores"] == .4 and values["cpu_time_seconds"] == 0
    assert call.cpu_time_seconds is None
    provenance = training_target_provenance(call)
    assert provenance["cpu_time_seconds"] == "imputed_zero"
    assert provenance["sampled_peak_rss_bytes"] != "imputed_zero"
    assert values["latency_ms"] == 10
    assert "timeout_lower_bound" not in provenance.values()


def test_zero_duration_is_imputed_but_numeric_zero_cpu_remains_observed():
    call = CompletedCall("r", "read", None, 1, 1, cpu_time_seconds=0, cpu_time_eligible=True)
    assert _target_values(call)["latency_ms"] == 0
    assert training_target_provenance(call)["latency_ms"] == "imputed_zero"
    assert training_target_provenance(call)["cpu_time_seconds"] == "observed"


def test_approximate_memory_is_not_replaced_and_lattice_reads_raw_value():
    from tool_time.resource_lattice import resource_values
    row = ClauseObservation("r", "read", ("read", "x"), 0, .02, latency_ms=20,
        memory_total_peak_bytes=123, memory_extra_peak_bytes=23, memory_eligible=False,
        censored=True)
    filled = apply_trace_label_policy_to_clause(row, STRICT_TRACE_LABEL_POLICY)
    assert filled.memory_total_peak_bytes == 123
    assert filled.cpu_ns_cumulative == 0 and "cpu_time_seconds" in filled.imputed_targets
    assert _clause_value(filled, "load:memory_total_peak_bytes") == 123
    assert resource_values(filled)["memory_total_peak_bytes"] == 123


def test_timeout_trace_round_trip_preserves_limit_avg_only_and_partial_values(tmp_path):
    request = _tool_request("before", "call", "sleep 30")
    event = _tool_completion("after", "call").model_copy(update={
        "confirmed_timeout": True, "timeout_duration_lower_bound_ms": 2500,
        "succeeded": False, "error_type": "timeout"})
    sample = replace(_runtime_sample("after", "call"), cpu_time_delta_s=None,
                     cpu_utilization_avg_cores=.25, sampling_quality="insufficient_samples")
    direct = completed_call_from_completion(event, sample, repo="r", start=request)
    writer = AgentTestBenchTraceWriter(tmp_path)
    writer.record_tool_started(request)
    writer.record_tool(event, sample)
    writer.close()
    loaded = [call for path in tmp_path.glob("*.jsonl") for call in load_openclaw_trace_observations(path, repo="r").completed_calls][0]
    assert loaded.censored and loaded.confirmed_timeout
    assert _target_values(loaded)["workload_latency_ms"] == 2500
    assert _target_values(loaded)["cpu_avg_cores"] == _target_values(direct)["cpu_avg_cores"] == .25
    assert _target_values(loaded)["sampled_peak_rss_bytes"] == sample.rss_bytes_peak


@pytest.mark.parametrize("before_hook,memory,stage", [
    (True, 123, "completion"), (True, None, "completion"), (False, 123, "completion"),
    (False, None, "close"), (False, None, "finalize"), (False, 123, "drain"), (True, None, "declared-source"),
])
def test_online_memory_target_is_submitted_once_without_duplicate_cpu_zero(tmp_path, monkeypatch, before_hook, memory, stage):
    import json
    from types import SimpleNamespace
    from test_execution_interruption_regressions import predictor_at
    predictor = predictor_at(tmp_path)
    request = _tool_request("before", "call", "sleep 30")
    if before_hook:
        predictor.record_tool_started(request)
    artifact_path = tmp_path / "execution.json"
    raw = {"bin": "sleep", "argv": ["sleep", "30"], "ts_start": 1000., "ts_end": 1001.,
           "latency_ms": 1000, "provenance": {"cpu_time_ns": 500000000}}
    call = {"command": "sleep 30", "clauses": [raw], "eligible_for_kb": True}
    artifact = {"version": 2, "mode": "clause", "calls": [call], "replay_execution": "completed"}
    artifact_path.write_text(json.dumps(artifact))
    run = SimpleNamespace(tool_call_id="call", _observer=SimpleNamespace(context=SimpleNamespace(artifact_path=artifact_path)))
    predictor._runs_by_execution_id["execution"] = run
    predictor._execution_owners["execution"] = ("g", "r")
    row = ClauseObservation("review", "sleep", ("sleep", "30"), 1000, 1001, latency_ms=1000,
                            cpu_ns_cumulative=500000000, cpu_peak_cores=0, sampled_peak_rss_mb=0)
    monkeypatch.setattr(predictor._sdk, "finish_command", lambda *args, **kwargs: SimpleNamespace(
        kb_observations_added=1, kb_observations=(row,), telemetry_artifact=artifact,
        call_telemetry=call, kb_update_error=None))
    try:
        predictor.finish_execution(execution_id="execution", exit_code=0, signal=None)
        assert "execution" in predictor._completion_inputs
        if stage in {"completion", "declared-source"}:
            def no_second_read(*args):
                raise AssertionError("completion must reuse the finalized artifact")
            monkeypatch.setattr("clawtune_sidecar.predictors.tool_resource._read_ebpf_artifact", no_second_read)
        sample = replace(_runtime_sample("after", "call"), environment_memory=(
            {"memory_total_peak_bytes": memory, "memory_extra_peak_bytes": memory} if memory is not None else None))
        if stage == "declared-source":
            sample = replace(sample, environment_memory={"memory_eligible": False,
                "memory_diagnostics": {"memory_measurement": "cgroup_v2_memory_current"}})
        event = _tool_completion("after", "call").model_copy(update={"execution_id": "execution"})
        if stage == "close":
            predictor.close()
        elif stage == "finalize":
            predictor.finalize_runtime("r", "g")
        elif stage == "drain":
            from fastapi.testclient import TestClient
            from clawtune_sidecar.api.app import create_app
            from clawtune_sidecar.api.dependencies import build_state
            from clawtune_sidecar.config import SidecarConfig
            state = build_state(SidecarConfig(trace_dir=tmp_path / "trace", tool_resource_ebpf_required=False))
            state.predictor.close()
            state.predictor = predictor
            monkeypatch.setattr(state.tool_monitor, "prepare", lambda: None)
            with TestClient(create_app(state)) as client:
                response = client.post("/v1/gateways/g/runtimes/r/drain?timeout_seconds=5&flush_kb=true")
                assert response.status_code == 200 and response.json()["drained"], response.text
                assert "execution" in predictor._pending_memory_targets
                predictor.observe_completion(event, sample)
        else:
            predictor.observe_completion(event, sample)
        assert "execution" not in predictor._completion_inputs
        if stage not in {"close", "drain"}:
            predictor.flush_kb_updates(timeout_seconds=5)
        saved = json.loads((tmp_path / "clause-lattice-time-kb.json").read_text())
        rows = saved["observations"] + saved["pending"]
        assert [r["cpu_ns_cumulative"] for r in rows if r["cpu_ns_cumulative"] is not None] == [500000000]
        assert [r["memory_total_peak_bytes"] for r in rows if r["memory_total_peak_bytes"] is not None] == [memory if memory is not None else 0]
        assert len(rows) == 2
        from tool_time.lattice_kb import LatticeTimeKB
        restored = LatticeTimeKB.from_json_obj(saved)
        targets = restored.predict_load_samples("review", [{"bin": "sleep", "argv": ["sleep", "30"],
            "memory_measurement": "cgroup_v2_memory_current" if stage == "declared-source" else "cgroup_v2_environment_union_v1"}], 2000)[0]
        assert targets["cpu_time_seconds"]["values"] == (.5,)
        assert targets["memory_total_peak_bytes"]["values"] == (memory if memory is not None else 0,)
    finally:
        predictor.close()


def test_missing_memory_measurement_zero_is_queryable_and_explicit_sources_stay_separate():
    from tool_resource.runtime_kb import RuntimeToolResourceKB, ToolCallQuery, ClauseResourceKB
    from tool_time.lattice_kb import LatticeTimeKB
    call = CompletedCall("r", "exec", "sleep 1", 0, 1)
    kb = RuntimeToolResourceKB.fit_public([call])
    assert kb.predict_load_samples(ToolCallQuery("r", "exec", "sleep 1", 2))["memory_total_peak_bytes"]["values"] == (0,)
    guest = kb.predict_load_samples(ToolCallQuery("r", "exec", "sleep 1", 2, memory_measurement="guest_memtotal_minus_memavailable"))
    assert "memory_total_peak_bytes" not in guest
    row = ClauseObservation("r", "sleep", ("sleep", "1"), 0, 1, latency_ms=1000,
                            memory_total_peak_bytes=0, memory_extra_peak_bytes=0,
                            imputed_targets=("memory_total_peak_bytes", "memory_extra_peak_bytes"))
    clause = {"bin": "sleep", "argv": ["sleep", "1"]}
    for clause_kb in (ClauseResourceKB.fit_public([row]), LatticeTimeKB.fit([row])):
        samples = clause_kb.predict_load_samples("r", [clause], 2)
        assert samples[0]["memory_total_peak_bytes"]["values"] == (0,)
        guest_samples = clause_kb.predict_load_samples("r", [dict(clause, memory_measurement="guest_memtotal_minus_memavailable")], 2)
        assert "memory_total_peak_bytes" not in guest_samples[0]


def test_missing_memory_keeps_declared_source_identical_online_and_replay(tmp_path):
    from tool_resource.runtime_kb import RuntimeToolResourceKB, ToolCallQuery
    event = _tool_completion("after", "call").model_copy(update={"tool_name": "write"})
    sample = replace(_runtime_sample("after", "call"), tool_name="write", environment_memory={
        "memory_eligible": False, "memory_unavailable_reason": "baseline_after_execution_start",
        "memory_diagnostics": {"memory_measurement": "cgroup_v2_memory_current",
                               "memory_environment_id": "container", "observed_baseline_bytes": 123}})
    direct = completed_call_from_completion(event, sample, repo="r")
    writer = AgentTestBenchTraceWriter(tmp_path)
    writer.record_tool(event, sample)
    writer.close()
    replay = [row for path in tmp_path.glob("*.jsonl") for row in load_openclaw_trace_observations(path, repo="r").completed_calls][0]
    assert direct.memory_measurement == replay.memory_measurement == "cgroup_v2_memory_current"
    assert direct.memory_total_peak_bytes is replay.memory_total_peak_bytes is None
    for row in (direct, replay):
        targets = RuntimeToolResourceKB.fit_public([row]).predict_load_samples(
            ToolCallQuery(row.repo, "write", None, row.ts_end+1, memory_measurement="cgroup_v2_memory_current"))
        assert targets["memory_total_peak_bytes"]["values"] == (0,)
