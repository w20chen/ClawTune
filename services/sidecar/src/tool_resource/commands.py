"""Shared shell extraction and executable-stage normalization."""
from __future__ import annotations
import posixpath
import re
import shlex
from typing import Any
from tool_resource.features import parse_command_clauses, shell_bin_requires_exec_evidence

def parse_execution(command: str | None, parser=parse_command_clauses) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Extract executable stages without discarding useful conditional clauses.

    AST supplies ownership/pipe structure. No shell text is evaluated here.
    Context and branch assumptions remain attached to the resulting clauses.
    """
    if not command:
        return (), "not_a_shell_command"
    parsed = parser(command)
    if parsed["parse_failed"]:
        return (), "parse_failed"
    output = []
    cwd = None
    environment = {}
    previous_end = 0
    previous_preparation = None
    for index, raw in enumerate(parsed["clauses"]):
        clause = dict(raw)
        argv = list(clause["argv"])
        if "span" in clause:
            start, end = clause["span"]
        else:
            original = raw.get("original") or shlex.join(argv)
            start = command.find(original, previous_end)
            if start < 0:
                return (), "parse_failed"
            end = start + len(original)
            clause["span"] = (start, end)
        gap = command[previous_end:start]
        previous_end = end
        assumptions = []
        if "||" in gap and previous_preparation is not None:
            cwd, environment = previous_preparation
        previous_preparation = None
        if "||" in gap:
            assumptions.append("conditional_on_previous_failure")
        elif "&&" in gap:
            assumptions.append("conditional_on_previous_success")
        context = clause.get("structural_context", [])
        if any(x.startswith(("if:", "for:", "while:", "until:", "case-item:", "stmt:background", "stmt:coprocess", "stmt:disown", "process-substitution", "command-substitution", "function", "subshell")) for x in context):
            clause["prediction_unavailable_reason"] = "dynamic_execution_structure"
        if clause.get("in_loop") or clause.get("in_subst"):
            clause["prediction_unavailable_reason"] = "dynamic_execution_structure"
        # Do not pretend a background or subshell program is foreground.
        if "structural_context" not in clause and any(v in gap for v in ("(", ")", "&")) and not re.fullmatch(r"[\s;&|]*", gap):
            clause["prediction_unavailable_reason"] = "dynamic_execution_structure"
        text = raw.get("original", command[start:end])
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = []
        local_env = dict(environment)
        for token in tokens:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
                break
            name, value = token.split("=", 1)
            local_env[name] = value
        if argv and argv[0] in {"export", "cd"} and (clause.get("prediction_unavailable_reason") or clause.get("in_pipe")):
            continue
        if argv and argv[0] == "export":
            previous_preparation = (cwd, dict(environment))
            for token in argv[1:]:
                if "=" in token:
                    name, value = token.split("=", 1)
                    environment[name] = value
            continue
        if argv and argv[0] == "cd":
            previous_preparation = (cwd, dict(environment))
            path = argv[-1] if len(argv) > 1 else "~"
            cwd = posixpath.normpath(posixpath.join(cwd or "", path))
            continue
        argv, wrapper_env, wrapper_assumptions, wrapper_reason = unwrap_argv(argv)
        local_env.update(wrapper_env)
        assumptions.extend(wrapper_assumptions)
        if wrapper_reason:
            clause["prediction_unavailable_reason"] = wrapper_reason
        if not argv or not shell_bin_requires_exec_evidence(posixpath.basename(argv[0]), argv[0]):
            continue
        clause.update(bin=posixpath.basename(argv[0]), argv=argv, clause_index=index,
                      cwd=cwd, env=local_env, prediction_assumptions=assumptions)
        output.append(clause)
    if output and re.search(r"(?<!&)&(?!&)[\s]*$", command):
        for clause in output:
            clause["prediction_unavailable_reason"] = "background_execution"
    return tuple(output), None if output else "no_executable_clause"



def unwrap_argv(raw):
    """Deterministic launch wrappers, shared by historical rows and queries."""
    argv, local_env, assumptions, reason = list(raw), {}, [], None
    while argv and posixpath.basename(argv[0]) in {"env", "nohup", "timeout"}:
        wrapper = posixpath.basename(argv.pop(0))
        if wrapper == "timeout":
            while argv and argv[0].startswith("-"):
                option = argv.pop(0)
                if option in {"-s", "--signal", "-k", "--kill-after"} and argv:
                    argv.pop(0)
            if argv:
                argv.pop(0)  # duration
            assumptions.append("execution_may_be_timeout_censored")
        elif wrapper == "env":
            while argv and ("=" in argv[0] or argv[0] == "--"):
                token = argv.pop(0)
                if "=" in token:
                    name, value = token.split("=", 1)
                    local_env[name] = value
            if argv and argv[0].startswith("-"):
                reason = "unsupported_env_option"
                break
    return argv, local_env, assumptions, reason


def normalized_observation(observation):
    from dataclasses import replace
    argv, _, _, reason = unwrap_argv(observation.argv)
    if not argv or reason:
        return observation
    return replace(observation, bin=posixpath.basename(argv[0]), argv=tuple(argv))
