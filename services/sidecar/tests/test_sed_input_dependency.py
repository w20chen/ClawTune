from dataclasses import replace

import pytest

from clawtune_sidecar.predictors.edge_kappa import training_event
from tool_resource.features import parse_command_clauses
from tool_resource.pipeline import pipe_stdin_from_syntax
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, is_pipeline_dependent_consumer
from tool_resource.sdk import _observations_from_call
from tool_time.lattice_kb import LatticeTimeKB


@pytest.mark.parametrize("command,dependent", [
    ("printf hello | sed -n '1,20p'", True),
    ("printf hello | /usr/bin/sed -n '1,30p'", True),
    ("sed -n '1,20p' file.txt", False),
    ("printf hello | sed -n '1,20p' file.txt", True),
    ("printf hello | sed -n -f script.sed", True),
    ("printf hello | sed -n '1,20p' < file.txt", True),
    ("printf hello | sed -n '1,20p' 0<&3", True),
    ("printf hello | sed -n '1,20p' <<EOF\nhello\nEOF", True),
    ("printf hello | sed 's/a/b/g'", True),
    ("sed -n '1,20p' file.txt | cat", False),
    ("printf hello | { sed -n '1,20p'; } < file.txt", True),
])
def test_sed_dependency_from_real_ast(command, dependent):
    parsed = parse_command_clauses(command)
    assert not parsed["parse_failed"]
    clause = next(row for row in parsed["clauses"] if row["bin"] == "sed")
    assert is_pipeline_dependent_consumer(clause) is dependent


def test_missing_input_evidence_does_not_exempt_downstream_sed():
    row = dict(bin="sed", argv=["sed", "-n", "1,20p"], in_pipe=True, pipeline_position=1)
    assert pipe_stdin_from_syntax(row) is None
    assert is_pipeline_dependent_consumer(row)


def test_missing_parser_does_not_manufacture_stdin_evidence(monkeypatch):
    from tool_resource.features import enrich_input_sources
    def unavailable(command):
        raise OSError("parser unavailable")
    monkeypatch.setattr("tool_resource.features.parse_command_clauses", unavailable)
    row = dict(bin="sed", argv=["sed", "-n", "1,20p"], in_pipe=True, pipeline_position=1)
    enriched = enrich_input_sources("printf hi | sed -n '1,20p'", [row])[0]
    assert enriched.get("stdin_from_pipe") is None
    assert is_pipeline_dependent_consumer(enriched)


def test_sed_admission_and_snapshot_use_same_evidence():
    row = ClauseObservation("repo", "sed", ("sed", "-n", "1,20p"), 1., 2.,
                            latency_ms=1000., in_pipe=True, pipeline_position=1,
                            stdin_from_pipe=True)
    assert training_event(row) is None
    assert not ClauseResourceKB().observe_completed_clause(row)
    assert not LatticeTimeKB().observe_completed_clause(row)
    unknown = replace(row, stdin_from_pipe=None)
    assert training_event(unknown) is None
    assert not ClauseResourceKB().observe_completed_clause(unknown)
    restored = LatticeTimeKB.from_json_obj(LatticeTimeKB.fit([unknown]).to_json_obj())
    assert restored.observation_count == 0
    standalone = replace(unknown, in_pipe=False, pipeline_position=-1)
    assert training_event(standalone) is not None
    assert ClauseResourceKB().observe_completed_clause(standalone)
    restored = LatticeTimeKB.from_json_obj(LatticeTimeKB.fit([standalone]).to_json_obj())
    assert restored.observation_count == 1


def test_sdk_and_recorded_loader_recover_same_input_evidence():
    from cold_start.flat_loader import recorded_clause_structure
    command = "printf hello | sed -n '1,20p'"
    row = dict(bin="sed", argv=["sed", "-n", "1,20p"], in_loop=False,
               in_subst=False, in_pipe=True, pipeline_position=1,
               ts_start=1., ts_end=2., latency_ms=1000., eligible_for_kb=True,
               telemetry_quality="ok", availability={"latency": "ok"})
    strict = recorded_clause_structure(command, [row], recorded_only=True)
    assert "stdin_from_pipe" not in strict[0]
    resolved = recorded_clause_structure(command, [row], recorded_only=True, resolve_input_sources=True)
    assert is_pipeline_dependent_consumer(resolved[0])
    observations = _observations_from_call("repo", {"command": command, "clauses": [row]}, require_timestamps=True)
    assert observations[0].stdin_from_pipe is True
    from clawtune_sidecar.predictors.tool_resource import _retained_workload_duration_seconds
    assert _retained_workload_duration_seconds({"calls": [dict(
        command=command, eligible_for_kb=True, clauses=[row])]
    }) is None


def test_repeated_argv_cannot_borrow_another_branches_input_source():
    from tool_resource.features import enrich_input_sources
    row = dict(bin="sed", argv=["sed", "-n", "1,20p"], in_pipe=True,
               pipeline_position=1)
    command = "printf a | sed -n '1,20p'; printf b | sed -n '1,20p' < file.txt"
    # The first branch may not have executed. An argv-only match is ambiguous.
    enriched = enrich_input_sources(command, [row])[0]
    assert enriched.get("stdin_from_pipe") is None
    assert is_pipeline_dependent_consumer(enriched)


def test_sed_does_not_add_waiting_time_to_pipeline_prediction_or_label():
    from clawtune_sidecar.predictors.call_load import compose
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from clawtune_sidecar.predictors.tool_resource import _retained_workload_duration_seconds
    clauses = [dict(bin="python", argv=["python", "job.py"], in_pipe=True,
                    pipeline_position=0, ts_start=1., ts_end=2.),
               dict(bin="sed", argv=["sed", "-n", "1,20p"], in_pipe=True,
                    pipeline_position=1, stdin_from_pipe=True, ts_start=1., ts_end=2.1)]
    result = compose("trie", [{"duration_ms": {"values": [1000.]}}],
                     load_bucket_edges((100, 500, 2000, 10000)), clauses=clauses)
    assert result.targets["duration_ms"].p50 == 1000.
    assert _retained_workload_duration_seconds({"calls": [{"eligible_for_kb": True, "clauses": clauses}]}) == 1.
