"""Public text API and KB adapters share the schema-driven representation."""
from __future__ import annotations

import shlex

import pytest

from command_parser import ParseContext, default_service
from command_parser.projection import atom
from tool_resource.runtime_kb import ClauseObservation
from tool_time._lattice_vendor.normalize import normalize_command
from tool_time.lattice_kb import LatticeTimeKB


@pytest.mark.parametrize("executable", ["/venv/bin/./python", "/usr/bin/python", "/env/bin/python3.12"])
def test_python_paths_share_command_features_without_losing_arguments(executable):
    command = " -m pytest " + " ".join(f"test{i}" for i in range(30))
    bare = normalize_command("python" + command)
    assert normalize_command(executable + command) == bare
    unresolved, core = normalize_command("$VENV/bin/python -m pytest")
    assert atom("target", "pytest") not in core
    assert any(f.startswith("opaque:") for f in unresolved)


@pytest.mark.parametrize("command,canonical,arguments", [
    ("pytest -q tests/unit", "--quiet", ["tests/unit"]),
    ("pytest -s tests/unit", "-s", ["tests/unit"]),
    ("pytest -x tests/unit", "--exitfirst", ["tests/unit"]),
    ("pytest --collect-only tests/unit", "--co", ["tests/unit"]),
    ("grep -n needle file", "--line-number", ["needle", "file"]),
    ("grep -i needle file", "--ignore-case", ["needle", "file"]),
    ("sed --quiet 1p file", "--quiet", ["1p", "file"]),
    ("pip install --no-deps package", "--no-deps", ["package"]),
    ("pip3 install --break-system-packages package", "--break-system-packages", ["package"]),
    ("pip install --no-cache-dir package", "--no-cache-dir", ["package"]),
])
def test_booleans_leave_following_arguments_separate(command, canonical, arguments):
    parsed = default_service().parser.parse(shlex.split(command), ParseContext())
    assert parsed.status == "complete"
    assert any(o["name"] == canonical and o["values"] == [] for o in parsed.layers[0]["options"])
    assert parsed.layers[0]["args"] == arguments


@pytest.mark.parametrize("prefix", ["python", "/venv/bin/python3.12", "python -u", "python -B -u",
                                    "python -Bu", "python -W ignore", "python -Wignore", "python -X dev"])
@pytest.mark.parametrize("module,args,target", [
    ("pytest", "-q tests/unit", ["pytest"]),
    ("pip", "install --no-deps package", ["pip", "install"]),
])
def test_python_module_owns_options_after_interpreter_prefix(prefix, module, args, target):
    features, core = normalize_command(f"{prefix} -m {module} {args}")
    assert core == {"tool=python", atom("target", *target)}
    expected = "--quiet" if module == "pytest" else "--no-deps"
    assert atom("flag", f"L1/{module}", expected) in features


@pytest.mark.parametrize("command,name,value", [
    ("pytest -k expression tests/unit", "-k", "expression"),
    ("pytest -m slow tests/unit", "-m", "slow"),
    ("pytest --maxfail 2 tests/unit", "--maxfail", "2"),
    ("pytest --maxfail=2 tests/unit", "--maxfail", "2"),
    ("grep -e needle file", "--regexp", "needle"),
    ("grep -f patterns.txt file", "--file", "patterns.txt"),
    ("grep -m 2 needle file", "--max-count", "2"),
    ("sed -e 's/a/b/' file", "--expression", "s/a/b/"),
    ("pip install -r requirements.txt", "--requirement", "requirements.txt"),
    ("pip install -e .", "--editable", "."),
])
def test_option_values_remain_bound(command, name, value):
    features, _ = normalize_command(command)
    tool = command.split()[0]
    assert atom("opt", f"L0/{tool}", name, [value]) in features


@pytest.mark.parametrize("command", ["pytest --unknown-plugin-option value", "git -n 2"])
def test_unknown_options_in_known_grammar_remain_opaque(command):
    features, _ = normalize_command(command)
    assert any(f.startswith("opaque:") for f in features)
    assert not any(f.startswith("opt:") for f in features)


def test_missing_grammar_uses_generic_value_heuristic():
    features, core = normalize_command("other -q value")
    assert core == {"tool=other"}
    assert features == {"tool=other", atom("opt", "L0/other", "-q", ["value"])}
    assert default_service().argv(["other", "-q", "value"]).status == "partial"


@pytest.mark.parametrize("command", ["python script.py -m pytest -q value", "python -u script.py -m pytest -q value",
                                    "python -c pass -m pytest -q value", "python -m other -q value"])
def test_script_or_other_module_does_not_inherit_pytest_flags(command):
    features, core = normalize_command(command)
    assert atom("target", "pytest") not in core
    assert not any(f.startswith("flag:") and "--quiet" in f for f in features)


@pytest.mark.parametrize("prefix", ["-Wignore", "-Xdev", "-Xutf8=1"])
def test_attached_interpreter_options_preserve_script_boundary(prefix):
    features, core = normalize_command(f"python {prefix} script.py -s value")
    assert core == {"tool=python", atom("target", "script.py")}
    assert atom("opt", "L0/python", prefix[:2], [prefix[2:]]) in features
    assert atom("flag", "L0/python", "-s") not in features


@pytest.mark.parametrize("code", ["print(1)", "x=1", "pass"])
def test_attached_python_code_ends_interpreter_options(code):
    features, core = normalize_command(shlex.join(["python", "-c" + code, "-s", "value"]))
    assert core == {"tool=python"}
    assert atom("mode", "inline") in features
    assert any(f.startswith("payload:") for f in features)
    assert atom("arg", "L0/python", 0, "-s") in features


def test_end_of_options_and_mixed_cluster():
    features, _ = normalize_command("pytest -- -q --no-header file")
    assert features == {"tool=pytest", atom("arg", "L0/pytest", 0, "-q"),
                        atom("arg", "L0/pytest", 1, "--no-header"), atom("arg", "L0/pytest", 2, "file")}
    features, _ = normalize_command("grep -ne needle file")
    assert atom("flag", "L0/grep", "--line-number") in features
    assert atom("opt", "L0/grep", "--regexp", ["needle"]) in features


@pytest.mark.parametrize("command", ["pytest -q tests/unit", "python -u -m pytest -q tests/unit",
                                    "other --jobs 8 input", "python task.py --jobs=8 input"])
def test_training_query_and_snapshot_reload_share_features(command):
    argv = tuple(shlex.split(command))
    row = ClauseObservation(repo="repo", bin=argv[0], argv=argv, ts_start=1., ts_end=2., latency_ms=50.)
    from command_parser.service import observation_features
    query = default_service().argv(argv, ParseContext(repo="repo"))
    assert observation_features(row).features == query.features
    lattice = LatticeTimeKB.from_json_obj(LatticeTimeKB.fit([row]).to_json_obj())
    lattice.prepare()
    assert query.features in lattice._nodes
