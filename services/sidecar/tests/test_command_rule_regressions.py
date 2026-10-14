"""Regression cases for verified bundled CLI syntax, independent of fallback."""
import pytest

from command_parser import CommandFeatureService, ParseContext


@pytest.fixture(scope="module")
def service():
    return CommandFeatureService()


@pytest.mark.parametrize("tail,operands", [
    (["-e", ".", "--no-deps"], []),
    (["-r", "requirements.txt"], []),
    (["--editable=.", "extra-package"], ["extra-package"]),
    (["--requirement=requirements.txt", "extra-package"], ["extra-package"]),
    (["extra-package", "-r", "requirements.txt"], ["extra-package"]),
])
def test_pip_install_option_targets_allow_additional_operands(service, tail, operands):
    parsed = service.parser.parse(["python", "-m", "pip", "install", *tail], ParseContext())
    assert parsed.status == "complete", parsed.diagnostics
    assert parsed.layers[-1]["args"] == operands


@pytest.mark.parametrize("tail", [[], ["--no-deps"], ["-e"], ["-r"], ["-r", "--no-deps"]])
def test_pip_still_rejects_missing_install_targets(service, tail):
    assert service.parser.parse(["pip", "install", *tail], ParseContext()).status == "invalid"


@pytest.mark.parametrize("tool", ["head", "tail"])
@pytest.mark.parametrize("tail,canonical,value", [
    (["-100", "file"], "--lines", "100"),
    (["-n", "-5", "file"], "--lines", "-5"),
    (["-c", "-5", "file"], "--bytes", "-5"),
])
def test_count_syntax_preserves_files(service, tool, tail, canonical, value):
    parsed = service.parser.parse([tool, *tail], ParseContext())
    assert parsed.status == "complete", parsed.diagnostics
    assert parsed.layers[0]["args"] == ["file"]
    assert [(o["name"], o["values"]) for o in parsed.layers[0]["options"]] == [(canonical, [value])]


@pytest.mark.parametrize("tool", ["head", "tail"])
def test_count_shorthand_is_rule_scoped_and_respects_terminator(service, tool):
    assert service.argv([tool, "-100", "file"]).features == service.argv([tool, "-n", "100", "file"]).features
    parsed = service.parser.parse([tool, "--", "-100"], ParseContext())
    assert parsed.layers[0]["args"] == ["-100"]
    assert not parsed.layers[0]["options"]
    assert service.parser.parse([tool, "-n", "-q"], ParseContext()).status == "invalid"
    assert service.parser.parse([tool, "-q", "-100"], ParseContext()).status == "partial"
    assert service.parser.parse(["pytest", "-100"], ParseContext()).status == "partial"


@pytest.mark.parametrize("tail,canonical,values,operands", [
    (["--check", "--", "file.py"], "--check", [], ["file.py"]),
    (["--stat", "HEAD"], "--stat", [], ["HEAD"]),
    (["--stat=80,40,10", "HEAD"], "--stat", ["80,40,10"], ["HEAD"]),
])
def test_git_diff_options_do_not_consume_revisions(service, tail, canonical, values, operands):
    parsed = service.parser.parse(["git", "diff", *tail], ParseContext())
    assert parsed.status == "complete", parsed.diagnostics
    assert parsed.target == ["diff"]
    assert parsed.layers[0]["args"] == operands
    assert next(o["values"] for o in parsed.layers[0]["options"] if o["name"] == canonical) == values


def test_pip_force_reinstall_is_boolean(service):
    parsed = service.parser.parse(["pip", "install", "--force-reinstall", "package"], ParseContext())
    assert parsed.status == "complete"
    assert parsed.layers[0]["args"] == ["package"]
    assert next(o["values"] for o in parsed.layers[0]["options"] if o["name"] == "--force-reinstall") == []


@pytest.mark.parametrize("argv,operands", [
    (["pip", "list", "--format=freeze"], []),
    (["pip", "list", "--format", "columns"], []),
    (["python", "-m", "pip", "--version"], []),
    (["pip", "show", "pytest", "numpy"], ["pytest", "numpy"]),
    (["git", "apply"], []),
    (["git", "apply", "-"], ["-"]),
    (["git", "show", "HEAD:setup.py"], ["HEAD:setup.py"]),
    (["git", "log", "--all", "--oneline", "--", "a.py", "b.py", "c.py"], ["a.py", "b.py", "c.py"]),
])
def test_observed_builtin_operand_and_option_grammars(service, argv, operands):
    parsed = service.parser.parse(argv, ParseContext())
    assert parsed.status == "complete", parsed.diagnostics
    assert parsed.layers[-1]["args"] == operands
    if "--format" in " ".join(argv):
        assert next(o["values"] for o in parsed.layers[-1]["options"] if o["name"] == "--format") in [["freeze"], ["columns"]]


@pytest.mark.parametrize("argv", [["pip", "list", "--format"], ["pip", "show"]])
def test_repaired_pip_commands_still_require_values(service, argv):
    assert service.parser.parse(argv, ParseContext()).status == "invalid"
