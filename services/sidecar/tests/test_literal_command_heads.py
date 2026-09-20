from copy import deepcopy
import sys

import pytest

from tool_resource.features import _resolve_literal_command_heads
from tool_resource.clause_bridge import ExecImageRecord, _alignment_evidence
from tool_resource.commands import parse_execution


def clause(command, text, head, *, dynamic=False):
    start = command.index(text)
    argv = [head, "-m", "pytest"] if head != "cd" else ["cd", "/workspace"]
    return dict(argv=argv, bin=head, span=(start, start + len(text)),
                in_loop=False, in_subst=False, in_pipe=False,
                structural_context=["subshell"] if dynamic else [],
                word_intents=[dict(cooked=word, span=(start, start + len(word)),
                    components=[dict(kind="parameter" if word.startswith("$") else "literal")])
                    for word in argv])


@pytest.mark.parametrize("head", ["$v", "${v}"])
def test_literal_head_is_shared_by_prediction_and_runtime_alignment(head):
    command = f"v=/tmp/v16/bin/python\ncd /workspace && {head} -m pytest"
    rows = [clause(command, "cd /workspace", "cd"), clause(command, f"{head} -m pytest", head)]
    _resolve_literal_command_heads(command, rows)
    assert rows[1]["argv"][0] == "/tmp/v16/bin/python"
    predicted, reason = parse_execution(command, parser=lambda _: dict(clauses=deepcopy(rows), parse_failed=False))
    assert reason is None
    assert predicted[0]["bin"] == "python"
    image = ExecImageRecord(host_pid=1, exec_seq=0, t_exec_ns=1, t_end_ns=2,
        bin="python", argv=("/tmp/v16/bin/python", "-m", "pytest"), terminal=True,
        cpu_windows=None, rss_bins=None, requested_executable_path="/tmp/v16/bin/python")
    assert _alignment_evidence(rows[1], image)[0] is not None
    from dataclasses import replace
    assert _alignment_evidence(rows[1], replace(image, requested_executable_path="/other/python"))[0] is None
    assert _alignment_evidence(rows[1], replace(image, argv=("/other/python", "-m", "pytest")))[0] is None


@pytest.mark.parametrize("prefix", ["v=$(which python); ", "v=$OTHER; ", "v=~/bin/python; ",
    "false && v=/tmp/python; ", "v=/tmp/python & ", "v=/tmp/python; unset v; "])
def test_unproven_assignments_do_not_resolve(prefix):
    command = prefix + "$v -m pytest"
    rows = [clause(command, "$v -m pytest", "$v")]
    _resolve_literal_command_heads(command, rows)
    assert rows[0]["argv"][0] == "$v"


def test_subshell_binding_is_not_reused():
    command = "v=/tmp/python; ($v -m pytest)"
    rows = [clause(command, "$v -m pytest", "$v", dynamic=True)]
    _resolve_literal_command_heads(command, rows)
    assert rows[0]["argv"][0] == "$v"


def test_shell_mutation_invalidates_earlier_binding():
    command = "v=/tmp/python; declare v=/other/python; $v -m pytest"
    mutation = clause(command, "declare v=/other/python", "declare")
    rows = [mutation, clause(command, "$v -m pytest", "$v")]
    _resolve_literal_command_heads(command, rows)
    assert rows[-1]["argv"][0] == "$v"


@pytest.mark.skipif(sys.platform == "win32", reason="bundled mvdan parser requires POSIX builder")
def test_native_parser_resolves_literal_head():
    from tool_resource.features import parse_command_clauses
    command = "v=/tmp/v16/bin/python\ncd /workspace && $v -m pytest"
    parsed = parse_command_clauses(command)
    assert not parsed["parse_failed"]
    assert parsed["clauses"][-1]["argv"] == ["/tmp/v16/bin/python", "-m", "pytest"]
