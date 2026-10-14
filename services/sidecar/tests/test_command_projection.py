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
    (["pytest", "-n8", "tests"], ["pytest", "--numprocesses=8", "tests"]),
    (["pytest", "-n=8", "tests"], ["pytest", "-n", "8", "tests"]),
    (["cargo", "t", "--release"], ["cargo", "test", "-r"]),
])
def test_equivalent_declared_forms(service, left, right):
    assert service.argv(left).features == service.argv(right).features


def test_operands_are_ordered_but_not_core(service):
    left = service.argv(["grep", "pattern", "file"])
    right = service.argv(["grep", "file", "pattern"])
    assert left.core == right.core == {"tool=grep"}
    assert left.features != right.features


def test_cargo_subcommands_have_distinct_core_identities(service):
    records = [service.argv(["cargo", entry]) for entry in ["fmt", "test", "build"]]
    assert len({record.core for record in records}) == 3
    for entry, record in zip(["fmt", "test", "build"], records):
        assert record.core == {"tool=cargo", atom("target", entry)}


def test_repeated_values_preserve_order(service):
    left = service.argv(["python", "-W", "ignore", "-W", "error", "-"])
    right = service.argv(["python", "-W", "error", "-W", "ignore", "-"])
    assert left.features != right.features


def test_budget_is_visible_and_keeps_core(service):
    record = service.argv(["python", "-m", "pytest", *map(str, range(30))])
    assert len(record.features) == 20
    assert record.core <= record.features
    assert record.omitted_features > 0


def test_long_command_keeps_workers_and_distinguishes_overflow_operands(service):
    paths = [f"tests/test_{i:02d}.py" for i in range(30)]
    prefix = ["python", "-m", "pytest", "-q", "-n"]
    left = service.argv([*prefix, "auto", *paths], ParseContext(repo="r"))
    right = service.argv([*prefix, "1", *paths], ParseContext(repo="r"))
    assert atom("opt", "L1/pytest", "--numprocesses", ["auto"]) in left.features
    assert atom("flag", "L1/pytest", "--quiet") in left.features
    assert left.features != right.features
    assert len(left.features) == 20
    overflow = [f for f in left.features if f.startswith("overflow:")]
    assert len(overflow) == 1
    # Changing a position that is not individually retained must change identity.
    index = next(i for i, path in enumerate(paths) if atom("arg", "L1/pytest", i, path) not in left.features)
    paths[index] += "::different_test"
    changed = service.argv([*prefix, "auto", *paths], ParseContext(repo="r"))
    assert left.features != changed.features
    assert CommandFeatures.from_dict(left.to_dict()) == left


def test_overflow_option_changes_are_not_lost(service):
    # Too many independent options still preserve all distinctions in overflow.
    argv = ["unknown-tool", *[f"--option-{i:02d}=value" for i in range(30)]]
    left = service.argv(argv)
    argv[-1] = "--option-29=changed"
    right = service.argv(argv)
    assert len(left.features) == len(right.features) == 20
    assert left.features != right.features


def test_small_budget_preserves_core_and_overflow_identity(service):
    left = service.argv(["python", "-m", "pytest", "a"], max_features=1)
    right = service.argv(["python", "-m", "pytest", "b"], max_features=1)
    assert left.core <= left.features
    assert left.features != right.features
    assert len(left.features) == len(left.core) + 1


def test_untruncated_features_do_not_gain_overflow(service):
    argv = ["python", "-m", "pytest", "-q", "-n", "8", "tests"]
    assert service.argv(argv).features == service.argv(argv, max_features=1000).features
    assert not any(f.startswith("overflow:") for f in service.argv(argv).features)


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
