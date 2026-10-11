from dataclasses import replace

import pytest

from command_parser import CommandFeatureService, ParseContext
from command_parser.models import CommandFeatures
from command_parser.projection import atom
from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB


@pytest.fixture(scope="module")
def service():
    return CommandFeatureService()


def test_interpreter_and_module_options_have_separate_owners(service):
    row = service.argv(["python", "-u", "-m", "pytest", "-m", "slow"])
    assert row.core == {"tool=python", atom("target", "pytest")}
    assert {atom("flag", "L0/python", "-u"), atom("opt", "L1/pytest", "-m", ["slow"])} <= row.features
    assert not any("revision" in value for value in row.features)
    assert CommandFeatures.from_dict(row.to_dict()) == row


@pytest.mark.parametrize("left,right", [
    (["pytest", "--maxfail=4"], ["pytest", "--maxfail", "4"]),
    (["pytest", "--collect-only"], ["pytest", "--co"]),
    (["grep", "-ne", "pattern", "file"], ["grep", "-n", "-e", "pattern", "file"]),
])
def test_equivalent_declared_forms(service, left, right):
    assert service.argv(left).features == service.argv(right).features


def test_operands_are_ordered_but_not_core(service):
    left = service.argv(["grep", "pattern", "file"])
    right = service.argv(["grep", "file", "pattern"])
    assert left.core == right.core == {"tool=grep"}
    assert left.features != right.features


def test_repeated_values_preserve_order(service):
    left = service.argv(["python", "-W", "ignore", "-W", "error", "-"])
    right = service.argv(["python", "-W", "error", "-W", "ignore", "-"])
    assert left.features != right.features


def test_budget_is_visible_and_keeps_core(service):
    record = service.argv(["python", "-m", "pytest", *map(str, range(30))])
    assert len(record.features) == 20
    assert record.core <= record.features
    assert record.omitted_features > 0


def test_rule_publication_does_not_change_bound_completion(tmp_path):
    service = CommandFeatureService()
    clauses = [{"argv": ["new-tool", "-n", "4"], "clause_index": 7}]
    first = service.prepare(clauses, ParseContext(repo="r"), "call")
    rule, _ = service.registry.lookup("head", ParseContext())
    rule.update(canonical="new-tool", names=["new-tool"])
    service.registry.add(rule, persist=False)
    second = service.prepare(clauses, ParseContext(repo="r"), "call")
    assert first == second
    assert service.prepare(clauses, ParseContext(repo="r"))[0]["command_features"] != first[0]["command_features"]
    row = ClauseObservation("r", "new-tool", tuple(clauses[0]["argv"]), 1, 2, latency_ms=1000, clause_index=7)
    bound = service.bind_observations([row], "call", release=True)[0]
    assert bound.command_features == first[0]["command_features"]
    kb = LatticeTimeKB.fit([bound])
    restored = LatticeTimeKB.from_json_obj(kb.to_json_obj())
    assert restored.to_json_obj()["observations"][0]["command_features"] == bound.command_features


def test_same_argv_clauses_require_identity_to_bind(service):
    clauses = [{"argv": ["python", "-"], "clause_index": i} for i in [0, 1]]
    service.prepare(clauses, ParseContext(repo="r"), "repeated")
    row = ClauseObservation("r", "python", ("python", "-"), 1, 2, latency_ms=1000)
    assert service.bind_observations([row], "repeated")[0].command_features is None
    assert service.bind_observations([replace(row, clause_index=1)], "repeated")[0].command_features
