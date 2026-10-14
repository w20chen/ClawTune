import pytest

from command_parser import CommandFeatureService, ParseContext
from command_parser.generation import GeneratorConfig, RuleGenerator
from command_parser.models import CommandFeatures
from command_parser.projection import atom
from command_parser.validation import validate


@pytest.fixture(scope="module")
def service():
    return CommandFeatureService()


def test_missing_rule_extracts_options_and_ordered_operands(service):
    argv = ["unknown-tool", "build", "--jobs", "8", "--mode=fast", "input.txt"]
    parsed = service.parser.parse(argv, ParseContext())
    validate("parsed-command", parsed.to_dict())
    assert parsed.status == "partial"
    assert parsed.target == []
    assert "missing_rule:unknown-tool" in parsed.diagnostics
    assert "heuristic_arguments:unknown-tool" in parsed.diagnostics
    assert parsed.layers[0]["args"] == ["build", "input.txt"]
    assert parsed.layers[0]["opaque"] == []
    row = service.argv(argv)
    assert row.core == {"tool=unknown-tool"}
    assert row.features == {
        "tool=unknown-tool", atom("opt", "L0/unknown-tool", "--jobs", ["8"]),
        atom("opt", "L0/unknown-tool", "--mode", ["fast"]),
        atom("arg", "L0/unknown-tool", 0, "build"),
        atom("arg", "L0/unknown-tool", 1, "input.txt"),
    }
    assert CommandFeatures.from_dict(row.to_dict()) == row


@pytest.mark.parametrize("tail", [
    ["-n", "8", "--mode=fast"], ["--mode", "fast", "-n=8"],
])
def test_generic_options_are_independently_matchable(service, tail):
    baseline = service.argv(["unknown-tool", "-n", "8", "--mode", "fast"])
    assert service.argv(["unknown-tool", *tail]).features == baseline.features
    changed = service.argv(["unknown-tool", "-n", "4", "--mode", "fast"])
    assert baseline.features & changed.features == {
        "tool=unknown-tool", atom("opt", "L0/unknown-tool", "--mode", ["fast"]),
    }


def test_generic_clusters_terminator_negative_values_and_stdin(service):
    parsed = service.parser.parse([
        "unknown-tool", "-xz", "archive", "--offset", "-2.5", "--input", "-",
        "--empty=", "--verbose", "--", "--literal", "-3", "-",
    ], ParseContext())
    layer = parsed.layers[0]
    assert [(o["name"], o["values"]) for o in layer["options"]] == [
        ("-x", []), ("-z", []), ("--offset", ["-2.5"]), ("--input", ["-"]),
        ("--empty", [""]), ("--verbose", []),
    ]
    assert layer["args"] == ["archive", "--literal", "-3", "-"]
    validate("parsed-command", parsed.to_dict())


def test_repeated_generic_values_keep_order_and_multiplicity(service):
    left = service.argv(["unknown-tool", "--item", "a", "--item", "b"])
    right = service.argv(["unknown-tool", "--item", "b", "--item", "a"])
    assert left.features != right.features
    assert atom("repeat", "L0/unknown-tool", "--item", [["a"], ["b"]]) in left.features
    assert service.argv(["unknown-tool", "-v", "-v"]).features != service.argv(["unknown-tool", "-v"]).features


@pytest.mark.parametrize("prefix,entry", [
    (["python", "script.py"], "script.py"),
    (["python", "-m", "unknown_module"], "unknown_module"),
])
def test_fallback_stays_inside_interpreter_entry(service, prefix, entry):
    argv = [*prefix, "-m", "slow", "--jobs", "8"]
    parsed = service.parser.parse(argv, ParseContext())
    assert parsed.target == [entry]
    assert parsed.layers[0]["options"] == []
    row = service.argv(argv)
    assert row.core == {"tool=python", atom("target", entry)}
    assert atom("opt", "L1/" + entry, "-m", ["slow"]) in row.features
    assert {"scope": "L1/" + entry, "kind": "option", "start": len(prefix),
            "end": len(prefix) + 2} in parsed.roles
    validate("parsed-command", parsed.to_dict())


@pytest.mark.parametrize("argv,index,opaque", [
    (["unknown-tool", "--jobs", "$N", "file"], 2, ["--jobs", "$N", "file"]),
    (["unknown-tool", "--jobs=$N", "file"], 1, ["--jobs=$N", "file"]),
    (["python", "script.py", "--jobs", "$N", "file"], 3, ["--jobs", "$N", "file"]),
    (["unknown-tool", "--", "$FILES", "file"], 2, ["$FILES", "file"]),
])
def test_expansion_stops_heuristic_consumption(service, argv, index, opaque):
    intents = [{"components": [{"kind": "parameter" if i == index else "literal"}]}
               for i in range(len(argv))]
    parsed = service.parser.parse(argv, ParseContext(), word_intents=intents)
    assert parsed.layers[-1]["options"] == []
    assert parsed.layers[-1]["opaque"] == opaque
    assert parsed.status == "partial"
    validate("parsed-command", parsed.to_dict())


def test_disabled_generator_still_uses_fallback(service):
    generator = RuleGenerator(service.registry, GeneratorConfig(),
                              client=lambda _: pytest.fail("unconfigured LLM called"))
    fallback = CommandFeatureService(service.registry, generator)
    try:
        row = fallback.argv(["unknown-tool", "--jobs", "8"])
        assert atom("opt", "L0/unknown-tool", "--jobs", ["8"]) in row.features
        assert generator.status() == {}
    finally:
        fallback.close()


def test_partial_parameter_match_reaches_lattice_and_edge(service):
    from edge_kappa_kb import EdgeKappaKB, TimeOutcome, TrainingEvent
    from tool_resource.runtime_kb import ClauseObservation
    from tool_time.edge_kappa_adapter import feature_query
    from tool_time.lattice_kb import LatticeTimeKB

    # The query changes --jobs but can still borrow the separate --mode feature.
    argv = ["unknown-tool", "--mode", "fast", "--jobs", "4"]
    query_argv = ["unknown-tool", "--mode", "fast", "--jobs", "8"]
    context = ParseContext(repo="r")
    features = service.argv(argv, context)
    query = service.argv(query_argv, context)
    shared = atom("opt", "L0/unknown-tool", "--mode", ["fast"])
    lattice = LatticeTimeKB.fit([
        ClauseObservation("r", argv[0], tuple(argv), 0, 1, latency_ms=1000,
                          command_features=features.to_dict()),
    ])
    clause = {"bin": argv[0], "argv": query_argv, "command_features": query.to_dict()}
    predictions = lattice.predict_clauses("r", [clause], 10)[0].predictions
    assert all(p.prediction_ms == 1000 and shared in p.selected_features for p in predictions)
    edge = EdgeKappaKB.fit([TrainingEvent(
        feature_query(features), TimeOutcome("e", 0, 1, "t", "c", "0", duration_ms=1000),
    )])
    prediction = edge.predict(feature_query(query))
    assert prediction.unavailable_reason is None
    assert any(shared in evidence.parent_features for evidence in prediction.evidence)
