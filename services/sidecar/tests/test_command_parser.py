import copy

import pytest
from jsonschema import ValidationError

from command_parser import CommandFeatureService, ParseContext
from command_parser.parser import CommandParser
from command_parser.registry import RuleRegistry
from command_parser.validation import validate, validate_rule


@pytest.fixture(scope="module")
def service():
    return CommandFeatureService()


@pytest.mark.parametrize("argv,target,mode,status", [
    (["python", "-u", "script.py"], ["script.py"], "script", "partial"),
    (["python", "-m", "pytest", "-m", "slow"], ["pytest"], "module", "complete"),
    (["python", "-c", "print(1)", "-m", "pytest"], [], "inline", "complete"),
    (["python", "-", "data"], [], "stdin", "complete"),
    (["python", "-Wignore", "-Xdev", "-m", "pytest", "-q"], ["pytest"], "module", "complete"),
    (["python", "-c"], [], "default", "invalid"),
    (["python", "-m"], [], "default", "invalid"),
    (["git", "-C", "repo", "status"], ["status"], "default", "complete"),
    (["env", "MODE=test", "python", "-m", "pytest"], ["python", "pytest"], "command", "complete"),
    (["timeout", "5", "python", "-", "x"], ["python"], "command", "complete"),
    (["grep", "-ne", "needle", "file"], [], "default", "complete"),
    (["pytest", "--", "-q"], [], "default", "complete"),
    (["unknown-tool", "--value", "x"], [], "default", "partial"),
])
def test_execution_boundaries(service, argv, target, mode, status):
    parsed = service.parser.parse(argv, ParseContext())
    assert parsed.target == target
    assert parsed.layers[0]["mode"] == mode
    assert parsed.status == status
    validate("parsed-command", parsed.to_dict())


def test_unknown_option_does_not_consume_guessed_value(service):
    parsed = service.parser.parse(["pytest", "--new-plugin-option", "-q", "file"], ParseContext())
    assert parsed.layers[0]["options"] == []
    assert parsed.layers[0]["opaque"] == ["--new-plugin-option", "-q", "file"]


def test_expansion_stops_static_argument_consumption(service):
    intents = [{"components": [{"kind": "literal"}]}, {"components": [{"kind": "parameter"}]}]
    parsed = service.parser.parse(["python", "$ARGS", "-m", "pytest"], ParseContext(), word_intents=intents)
    assert parsed.target == []
    assert "unresolved_expansion" in parsed.diagnostics


def test_literal_dash_value_and_missing_value(service):
    assert service.parser.parse(["python", "-c", "-x"], ParseContext()).status == "complete"
    assert service.parser.parse(["pytest", "-m", "-q"], ParseContext()).status == "invalid"


def test_schema_rejects_executable_rule_and_alias_collision(service):
    rule, _ = service.registry.lookup("pytest", ParseContext())
    unsafe = copy.deepcopy(rule)
    unsafe["callback"] = "execute arbitrary code"
    with pytest.raises(ValidationError):
        validate_rule(unsafe)
    rule["grammar"]["options"].append(copy.deepcopy(rule["grammar"]["options"][0]))
    with pytest.raises(ValueError, match="duplicate option"):
        validate_rule(rule)


def test_rule_order_does_not_change_resolution(service):
    rule, _ = service.registry.lookup("python", ParseContext())
    registry = RuleRegistry(bundled=False)
    registry.add(rule, persist=False)
    rule["grammar"]["options"].clear()
    parsed = CommandParser(registry).parse(["python", "-m", "pytest"], ParseContext())
    assert parsed.target == ["pytest"]


def test_optional_numeric_value_does_not_swallow_make_target(service):
    parsed = service.parser.parse(["make", "-j", "check"], ParseContext())
    assert parsed.layers[0]["options"][0]["values"] == []
    assert parsed.layers[0]["args"] == ["check"]
    numeric = service.parser.parse(["make", "-j", "4", "check"], ParseContext())
    assert numeric.layers[0]["options"][0]["values"] == ["4"]


def test_sed_program_is_not_an_execution_entry(service):
    assert service.parser.parse(["sed", "d", "file"], ParseContext()).target == []


def test_word_roles_preserve_parent_child_boundaries(service):
    parsed = service.parser.parse(["python", "-m", "pytest", "-m", "slow"], ParseContext())
    roles = parsed.roles
    assert {"scope": "L0/python", "kind": "entry_option", "start": 1, "end": 3} in roles
    assert {"scope": "L1/pytest", "kind": "option", "start": 3, "end": 5} in roles


def test_script_identity_is_relative_to_registered_workspace(service, tmp_path):
    sub = tmp_path / "scripts"
    sub.mkdir()
    (sub / "task.py").write_text("print(1)")
    context = ParseContext(workspace=str(tmp_path), cwd=str(sub))
    first = service.argv(["python", "task.py"], context)
    second = service.argv(["python", str(sub / "task.py")], context)
    assert first.features == second.features
    assert 'target:["scripts/task.py"]' in first.core


@pytest.mark.parametrize("workers", [["-n", "auto"], ["-n", "8"], ["-n8"], ["-n=8"],
                                    ["--numprocesses", "8"], ["--numprocesses=8"]])
def test_pytest_workers_preserve_following_options_and_paths(service, workers):
    argv = ["python", "-m", "pytest", "-q", *workers, "--tb=short", "tests/devices", "tests/other"]
    parsed = service.parser.parse(argv, ParseContext())
    assert parsed.status == "complete"
    assert parsed.target == ["pytest"]
    layer = parsed.layers[-1]
    assert layer["opaque"] == []
    assert layer["args"] == ["tests/devices", "tests/other"]
    options = {o["name"]: o["values"] for o in layer["options"]}
    assert options["--numprocesses"] == ["auto" if "auto" in workers else "8"]
    assert options["--tb"] == ["short"]


@pytest.mark.parametrize("argv", [["pytest", "-n"], ["pytest", "-n", "--tb=short"]])
def test_pytest_workers_require_a_value(service, argv):
    parsed = service.parser.parse(argv, ParseContext())
    assert parsed.status == "invalid"
    assert "missing_option_value" in parsed.diagnostics


@pytest.mark.parametrize("argv,target", [
    (["cargo", "fmt", "--all", "--check"], "fmt"),
    (["cargo", "test", "--test", "integration", "filter"], "test"),
    (["cargo", "t", "-p", "my-package", "filter"], "test"),
    (["cargo", "build", "--release", "--jobs=4"], "build"),
    (["cargo", "check", "--all-targets"], "check"),
])
def test_cargo_known_entries(service, argv, target):
    parsed = service.parser.parse(argv, ParseContext())
    assert parsed.status == "complete"
    assert parsed.target == [target]
    validate("parsed-command", parsed.to_dict())


@pytest.mark.parametrize("entry", ["test", "fmt"])
def test_cargo_forwarded_tail_is_not_parsed_as_cargo_options(service, entry):
    parsed = service.parser.parse(["cargo", entry, "--", "--check", "--test-threads", "3"], ParseContext())
    assert parsed.status == "complete"
    assert parsed.target == [entry]
    assert parsed.layers[0]["options"] == []
    assert parsed.layers[0]["args"] == ["--check", "--test-threads", "3"]


def test_unknown_cargo_extension_is_not_guessed_as_entry(service):
    parsed = service.parser.parse(["cargo", "unseen-plugin", "--flag"], ParseContext())
    assert parsed.status == "partial"
    assert parsed.target == []
    assert parsed.layers[0]["opaque"] == ["unseen-plugin", "--flag"]


def test_short_equals_is_not_stripped_without_declared_form(service):
    rule, _ = service.registry.lookup("head", ParseContext())
    option = next(o for o in rule["grammar"]["options"] if "-n" in o["names"])
    option["forms"] = ["separate", "attached"]
    registry = RuleRegistry(bundled=False)
    registry.add(rule, persist=False)
    parsed = CommandParser(registry).parse(["head", "-n=8", "file"], ParseContext())
    assert parsed.layers[0]["options"][0]["values"] == ["=8"]


@pytest.mark.parametrize("argv,spelling,values,operands", [
    (["git", "config", "--local", "user.name", "Example"], "--local", [], ["user.name", "Example"]),
    (["git", "config", "--local", "--get", "user.name"], "--local", [], ["user.name"]),
    (["git", "config", "--local", "--list"], "--local", [], []),
    (["git", "merge", "-s", "ort", "topic"], "--strategy", ["ort"], ["topic"]),
    (["git", "rebase", "-s", "ort", "main"], "--strategy", ["ort"], ["main"]),
    (["git", "pull", "-s", "ort", "origin", "main"], "--strategy", ["ort"], ["origin", "main"]),
    (["git", "branch", "-d", "topic1", "topic2"], "--delete", [], ["topic1", "topic2"]),
    (["npm", "install", "-w", "workspace-a", "package-b"], "--workspace", ["workspace-a"], ["package-b"]),
])
def test_completion_arguments_do_not_swallow_command_operands(service, argv, spelling, values, operands):
    parsed = service.parser.parse(argv, ParseContext())
    assert parsed.status == "complete"
    layer = parsed.layers[-1]
    assert next(o["values"] for o in layer["options"] if o["name"] == spelling) == values
    assert layer["args"] == operands
    assert not layer["opaque"]
