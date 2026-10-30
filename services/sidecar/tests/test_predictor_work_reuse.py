"""Guard reuse boundaries: identical results, fresh inputs and bounded retention."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from clawtune_sidecar.predictors.tool_resource import ExecutionTelemetrySummary, ToolResourcePredictor, _completion_artifact
from command_parser.models import ParseContext
from command_parser.registry import RuleRegistry
from command_parser.service import CommandFeatureService
from tool_resource.runtime_kb import ClauseObservation, LatencyBuckets
from test_execution_interruption_regressions import predictor_at
from test_tool_resource_predictor import _runtime_sample, _tool_completion, _tool_request


def test_advancing_without_predictions_preserves_strict_causality():
    from tool_resource.runtime_kb import CompletedCall, RuntimeToolResourceKB, ToolCallQuery
    kb = RuntimeToolResourceKB()
    kb.observe_completed_call(CompletedCall("r", "exec", "sleep 1", 0, 1))
    query = ToolCallQuery("r", "exec", "sleep 1", 1)
    with patch.object(kb, "_predict_target", side_effect=AssertionError("advance must not predict")):
        kb._advance(1)
        assert not kb.predict_load_samples(query)
        kb._advance(2)
        assert kb.predict_load_samples(replace(query, ts_start=2))["duration_ms"]["values"] == (1000,)
        with pytest.raises(ValueError, match="backdated query"):
            kb._advance(1)
        kb.freeze()
        kb._advance(0)
        assert kb.predict_load_samples(replace(query, ts_start=0))["duration_ms"]["values"] == (1000,)


@pytest.mark.parametrize("command", ["python -m pytest -q", "git status | cat", "git status && git diff", "for x in a b; do echo $x; done"])
def test_online_prediction_parses_once_and_only_queries_retained_diagnostics(tmp_path, command):
    from tool_resource.features import get_mvdan_client
    predictor = predictor_at(tmp_path)
    try:
        with patch.object(get_mvdan_client(), "parse", wraps=get_mvdan_client().parse) as parse, \
             patch.object(predictor.continuous_kb, "query", side_effect=AssertionError("discarded full prediction")), \
             patch.object(predictor.continuous_kb, "_predict_target", wraps=predictor.continuous_kb._predict_target) as target, \
             patch.object(predictor, "_kv_ttl_cost_payload", wraps=predictor._kv_ttl_cost_payload) as ttl:
            result = predictor.predict(_tool_request("before", "call", command))
        assert result.tool is not None and result.trie is not None and result.lattice is not None
        assert parse.call_count == 1
        assert target.call_count == 4
        assert ttl.call_count <= 1
    finally:
        predictor.close()


def test_script_source_is_shared_within_lookup_but_changes_are_visible():
    registry = RuleRegistry(bundled=False)
    registry._rules = {str(i): {"scope": {"kind": "script", "repo": "r", "runtime_id": "rt",
        "entry": "job.py", "source_hash": str(i)}, "names": ["job.py"]} for i in range(3)}
    context = ParseContext(repo="r", runtime_id="rt")
    with patch("command_parser.evidence.source", side_effect=[{"content_hash": "1"}, {"content_hash": "2"}, None]) as source:
        assert registry._lookup_for_parse("job.py", context, script=True)[1] == "1"
        assert registry._lookup_for_parse("job.py", context, script=True)[1] == "2"
        assert registry._lookup_for_parse("job.py", context, script=True) == (None, None)
        assert source.call_count == 3
        assert registry._lookup_for_parse("job.py", ParseContext(repo="other", runtime_id="rt"), script=True) == (None, None)
        assert source.call_count == 3


def test_binding_keeps_index_and_ambiguity_semantics_without_cross_product():
    from tool_resource import commands
    service = CommandFeatureService(registry=RuleRegistry(bundled=False))
    service._calls["call"] = {i: (("env", "X=1", "python", "job.py"), {"index": i}) for i in range(20)}
    row = ClauseObservation("r", "python", ("python", "job.py"), 0, 1, latency_ms=1000)
    rows = [replace(row, clause_index=i) for i in range(20)]
    with patch.object(commands, "unwrap_argv", wraps=commands.unwrap_argv) as unwrap:
        bound = service.bind_observations(rows + rows, "call")
    assert [r.command_features for r in bound] == [{"index": i} for i in range(20)] * 2
    assert unwrap.call_count == 20
    assert service.bind_observations([row], "call") == [row]  # identical argv is ambiguous without an index
    wrong = replace(row, clause_index=99)
    assert service.bind_observations([wrong], "call", release=True) == [wrong]
    assert "call" not in service._calls


def test_seed_bytes_are_validated_and_loaded_once():
    from clawtune_kb.contracts import data_root
    from clawtune_kb.store import FILES
    seed = (data_root() / "seeds/bootstrap-v2").resolve()
    reads = []
    original = Path.open
    def read(path, *args, **kwargs):
        if path.parent == seed and path.name in FILES:
            reads.append(path.name)
        return original(path, *args, **kwargs)
    with patch.object(Path, "open", read):
        predictor = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
            artifact_dir=seed, frozen=True, buckets=LatencyBuckets((100, 500, 2000)))
    try:
        assert sorted(reads) == sorted(FILES)
        with patch.object(predictor, "_kv_ttl_cost_payload", wraps=predictor._kv_ttl_cost_payload) as ttl:
            assert predictor.predict(_tool_request("before", "call", "python -m pytest -q")).lattice is not None
        assert ttl.call_count == 1
    finally:
        predictor.close()


def test_schema_registry_is_reused_and_root_isolated(tmp_path, monkeypatch):
    from clawtune_kb import contracts
    from jsonschema import ValidationError
    roots = [tmp_path / "one", tmp_path / "two"]
    for root, kind in zip(roots, ("integer", "string")):
        (root / "contracts").mkdir(parents=True)
        (root / "contracts" / "value.schema.json").write_text(json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:test:value", "type": kind}))
    monkeypatch.setattr(contracts, "data_root", lambda: roots[0])
    with patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
        contracts.validate(1, "value.schema.json")
        contracts.validate(2, "value.schema.json")
        assert read.call_count == 1
    monkeypatch.setattr(contracts, "data_root", lambda: roots[1])
    contracts.validate("value", "value.schema.json")
    with pytest.raises(ValidationError):
        contracts.validate(1, "value.schema.json")


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_completion_handoff_matches_disk_labels_and_duration(tmp_path, monkeypatch, frozen, interrupted):
    outputs = []
    artifact = {"version": 2, "mode": "clause", "calls": [{"command": "sleep 1; sleep 2", "clauses": [
        {"bin": "sleep", "argv": ["sleep", str(i + 1)], "clause_index": i,
         "ts_start": 1000. + i, "ts_end": 1001. + i, "memory_total_peak_bytes": 100 + i,
         "memory_extra_peak_bytes": i, "raw_events": ["large data"]} for i in range(2)]}]}
    projected = _completion_artifact(artifact, tmp_path / "execution.json")
    assert all("raw_events" not in row for row in projected["calls"][0]["clauses"])
    artifact["calls"][0]["clauses"][0]["argv"].append("changed")
    assert projected["calls"][0]["clauses"][0]["argv"] == ["sleep", "1"]
    artifact["calls"][0]["clauses"][0]["argv"].pop()
    for use_handoff in (False, True):
        directory = tmp_path / str(use_handoff)
        directory.mkdir()
        predictor = predictor_at(directory)
        predictor.frozen = frozen
        path = directory / "execution.json"
        path.write_text(json.dumps(artifact))
        run = SimpleNamespace(tool_call_id="call", _observer=SimpleNamespace(context=SimpleNamespace(artifact_path=path)))
        predictor._runs_by_execution_id["execution"] = run
        predictor._execution_owners["execution"] = ("g", "r")
        monkeypatch.setattr(predictor._sdk, "finish_command", lambda *a, **kw: SimpleNamespace(
            kb_observations_added=0, kb_observations=(), telemetry_artifact=artifact, call_telemetry=None, kb_update_error=None))
        try:
            summary = predictor.finish_execution(execution_id="execution", exit_code=0, signal=None,
                incomplete_reason="tool_timeout" if interrupted else None, confirmed_timeout=interrupted,
                timeout_duration_lower_bound_ms=3000 if interrupted else None)
            assert "_completion_inputs" not in summary.__dataclass_fields__
            if use_handoff:
                path.unlink()  # completion must not depend on reopening the finalized file
            else:
                predictor._completion_inputs.clear()
            sample = replace(_runtime_sample("after", "call"), environment_memory=None)
            event = _tool_completion("after", "call").model_copy(update={"execution_id": "execution"})
            with patch.object(predictor.continuous_kb, "observe_completed_call") as observe:
                predictor.observe_completion(event, sample)
            assert "execution" not in predictor._completion_inputs
            retained = predictor.execution_telemetry("execution")
            assert retained.call_telemetry is None
            assert retained.confirmed_timeout == interrupted
            assert retained.replay_execution == summary.replay_execution
            assert "execution" in predictor._completed_execution_ids
            assert "execution" not in predictor._execution_owners
            assert len(sample.environment_memory["memory_clause_observations"]) == 2
            outputs.append((sample.environment_memory, observe.call_args))
        finally:
            predictor.close()
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("artifact", [None, {}, {"version": 2, "mode": "bad", "calls": []},
    {"version": 2, "mode": "clause", "calls": [{"clauses": None}]}])
def test_invalid_completion_input_keeps_reader_fallback(tmp_path, artifact):
    assert _completion_artifact(artifact, tmp_path / "execution.json") is None


def test_completion_handoff_protects_pending_work_and_bounds_completed_history(tmp_path):
    predictor = predictor_at(tmp_path)
    try:
        predictor._completion_inputs = {str(i): (("g", "r"), {"calls": []}) for i in range(10_001)}
        predictor._execution_owners = dict.fromkeys(predictor._completion_inputs, ("g", "r"))
        predictor._telemetry_by_execution_id = {key: ExecutionTelemetrySummary(
            key, "call", None, True, "ok", replay_execution="completed",
            call_telemetry={"clauses": [{"argv": ["sleep", "1"]}]}) for key in predictor._completion_inputs}
        predictor._trim_execution_telemetry()
        assert len(predictor._completion_inputs) == 10_001
        assert "0" in predictor._telemetry_by_execution_id
        predictor._completion_inputs["other"] = (("g", "other-runtime"), {"calls": []})
        predictor._execution_owners["other"] = ("g", "other-runtime")
        predictor.finalize_runtime("r", "g")
        assert list(predictor._completion_inputs) == ["other"]
        assert predictor._execution_owners == {"other": ("g", "other-runtime")}
        assert len(predictor._completed_execution_ids) == 10_000
        assert "0" not in predictor._telemetry_by_execution_id
        assert all(row.call_telemetry is None for row in predictor._telemetry_by_execution_id.values())
    finally:
        predictor.close()
    assert not predictor._completion_inputs
    assert not predictor._telemetry_by_execution_id
    assert not predictor._completed_execution_ids
