"""Small boolean-option policy, including boundaries that must still take values."""
from __future__ import annotations

import shlex

import pytest

from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime, training_event
from tool_resource.runtime_kb import ClauseObservation
from tool_time._lattice_vendor.normalize import normalize_command
from tool_time.edge_kappa_adapter import shell_query
from tool_time.lattice_kb import LatticeTimeKB


@pytest.mark.parametrize("executable", ["/venv/bin/./python", "/usr/bin/python", "/env/bin/python3.12"])
def test_python_paths_share_command_features_without_losing_arguments(executable):
    bare, core = normalize_command("python -m pytest " + " ".join(f"test{i}" for i in range(30)))
    explicit, explicit_core = normalize_command(executable + " -m pytest " + " ".join(f"test{i}" for i in range(30)))
    assert explicit == bare
    assert explicit_core == core
    assert not any(f.startswith("executable_path=") for f in bare)
    unresolved, _ = normalize_command("$VENV/bin/python -m pytest")
    assert not any(f.startswith("executable_path=") for f in unresolved)


@pytest.mark.parametrize("version", ["shell-normalize-v1", "shell-normalize-v2", "shell-normalize-v3"])
def test_edge_runtime_rejects_old_normalization_at_load(version):
    snapshot = EdgeKappaRuntime.fit([], (100, 500, 2000, 10000)).to_snapshot()
    snapshot["normalization_version"] = version
    with pytest.raises(ValueError, match="rebuild the KB"):
        EdgeKappaRuntime.from_snapshot(snapshot)


@pytest.mark.parametrize(("command", "flags", "arguments"), [
    ("pytest -q tests/unit", ["-q"], ["tests/unit"]),
    ("pytest -s tests/unit", ["-s"], ["tests/unit"]),
    ("pytest -x tests/unit", ["-x"], ["tests/unit"]),
    ("pytest --no-flaky-report tests/unit", ["--no-flaky-report"], ["tests/unit"]),
    ("pytest --collect-only tests/unit", ["--collect-only"], ["tests/unit"]),
    ("pytest --disable-warnings tests/unit", ["--disable-warnings"], ["tests/unit"]),
    ("pytest -qxs tests/unit", ["-q", "-x", "-s"], ["tests/unit"]),
    ("pytest -qq tests/unit", ["-q"], ["tests/unit"]),
    ("grep -R needle src", ["-R"], ["needle", "src"]),
    ("grep -n needle file", ["-n"], ["needle", "file"]),
    ("grep -i needle file", ["-i"], ["needle", "file"]),
    ("grep -rin needle src", ["-r", "-i", "-n"], ["needle", "src"]),
    ("grep --ignore-case needle file", ["--ignore-case"], ["needle", "file"]),
    ("sed -n 1p file", ["-n"], ["1p", "file"]),
    ("sed -E 's/a/b/' file", ["-E"], ["s/a/b/", "file"]),
    ("sed --quiet 1p file", ["--quiet"], ["1p", "file"]),
    ("pip install --break-system-packages package", ["--break-system-packages"], ["package"]),
    ("pip3 install --no-deps package", ["--no-deps"], ["package"]),
    ("pip install --no-cache-dir package", ["--no-cache-dir"], ["package"]),
    ("/venv/bin/pytest -q 'tests/unit case'", ["-q"], ["tests/unit case"]),
])
def test_known_booleans_leave_following_arguments_separate(command, flags, arguments):
    features, _ = normalize_command(command)
    assert {f"flag:{flag}" for flag in flags} <= features
    assert {f"arg{i}={arg}" for i, arg in enumerate(arguments)} <= features
    for flag in flags:
        assert not any(feature.startswith(f"opt:{flag}=") for feature in features)


@pytest.mark.parametrize("prefix", [
    "python", "/venv/bin/python3.12", "python -u", "python -B -u", "python -Bu",
    "python -W ignore", "python -Wignore", "python -X dev",
])
@pytest.mark.parametrize(("module", "args", "flag", "argument"), [
    ("pytest", "-q tests/unit", "-q", "arg0=tests/unit"),
    ("pip", "install --break-system-packages package", "--break-system-packages", "arg1=package"),
])
def test_python_module_uses_module_options_after_interpreter_prefix(prefix, module, args, flag, argument):
    features, core = normalize_command(f"{prefix} -m {module} {args}")
    assert core == frozenset({"tool=python", f"target={module}"})
    assert {f"flag:{flag}", argument, f"opt:-m={module}"} <= features


@pytest.mark.parametrize(("command", "option", "value"), [
    ("pytest -k expression tests/unit", "-k", "expression"),
    ("pytest -m slow tests/unit", "-m", "slow"),
    ("pytest --maxfail 2 tests/unit", "--maxfail", "2"),
    ("pytest --maxfail=2 tests/unit", "--maxfail", "2"),
    ("grep -e needle file", "-e", "needle"),
    ("grep -f patterns.txt file", "-f", "patterns.txt"),
    ("grep -m 2 needle file", "-m", "2"),
    ("sed -e 's/a/b/' file", "-e", "s/a/b/"),
    ("sed -f script.sed file", "-f", "script.sed"),
    ("pip install -r requirements.txt", "-r", "requirements.txt"),
    ("pip install -e .", "-e", "."),
    ("python -m pip install -r requirements.txt", "-r", "requirements.txt"),
    ("git -n 2", "-n", "2"),
    ("other -q value", "-q", "value"),
])
def test_value_options_and_other_tools_keep_their_values(command, option, value):
    features, _ = normalize_command(command)
    assert f"opt:{option}={value}" in features
    assert f"flag:{option}" not in features


@pytest.mark.parametrize("command", [
    "python script.py -m pytest -q value",
    "python -u script.py -m pytest -q value",
    "python -c pass -m pytest -q value",
    "python -m other -q value",
])
def test_python_script_or_other_module_does_not_inherit_pytest_flags(command):
    features, _ = normalize_command(command)
    assert "opt:-q=value" in features
    assert "flag:-q" not in features
    assert "target=pytest" not in features


@pytest.mark.parametrize("command", [
    "python script.py -s value", "python -u script.py -s value", "python -c pass -s value",
])
def test_interpreter_flags_do_not_leak_into_script_arguments(command):
    features, _ = normalize_command(command)
    assert "opt:-s=value" in features
    assert "flag:-s" not in features


@pytest.mark.parametrize("prefix", ["-Wignore", "-Xdev", "-Xutf8=1"])
def test_attached_interpreter_options_preserve_script_boundary(prefix):
    features, _ = normalize_command(f"python {prefix} script.py -s value")
    assert f"opt:{prefix[:2]}={prefix[2:]}" in features
    assert "arg0=script.py" in features
    assert "opt:-s=value" in features
    assert "flag:-s" not in features


@pytest.mark.parametrize("code", ["print(1)", "x=1", "pass"])
def test_attached_python_code_ends_interpreter_options(code):
    features, _ = normalize_command(shlex.join(["python", "-c" + code, "-s", "value"]))
    assert f"opt:-c={code}" in features
    assert "opt:-s=value" in features
    assert "flag:-s" not in features


def test_attached_interpreter_options_use_module_flags_only_after_module():
    features, core = normalize_command("python -Wignore -Xdev -m pytest -s tests/unit")
    assert core == frozenset({"tool=python", "target=pytest"})
    assert {"opt:-W=ignore", "opt:-X=dev", "flag:-s", "arg0=tests/unit"} <= features
    # These spellings belong to the script after its name, not Python.
    features, _ = normalize_command("python script.py -Wignore value")
    assert "opt:-Wignore=value" in features


def test_end_of_options_and_mixed_clusters_are_not_boolean_flags():
    features, _ = normalize_command("pytest -- -q --no-header file")
    assert features == frozenset({
        "tool=pytest", "arg0=-q", "arg1=--no-header", "arg2=file",
    })
    features, _ = normalize_command("grep -ne needle file")
    assert "opt:-ne=needle" in features
    assert "flag:-n" not in features
    assert "flag:-e" not in features


@pytest.mark.parametrize("command", ["pytest -q tests/unit", "python -u -m pytest -q tests/unit"])
def test_training_query_and_snapshot_reload_share_boolean_features(command):
    argv = tuple(shlex.split(command))
    row = ClauseObservation(repo="repo", bin=argv[0], argv=argv,
                            ts_start=1., ts_end=2., latency_ms=50.)
    query = shell_query(command, repo="repo")
    event = training_event(row)
    assert event is not None and event.query == query
    lattice = LatticeTimeKB.from_json_obj(LatticeTimeKB.fit([row]).to_json_obj())
    lattice.prepare()
    assert query.features in lattice._nodes
    assert {"flag:-q", "arg0=tests/unit"} <= query.features
    edge = EdgeKappaRuntime.from_snapshot(
        EdgeKappaRuntime.fit([row], (100, 500, 2000, 10000)).to_snapshot(), frozen=True,
    )
    result = edge.predict_load_samples("repo", [{"argv": argv}], 3.)
    assert result[0]["duration_ms"]["values"] == (50.,)
