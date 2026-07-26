from __future__ import annotations

import json
import re
import shlex
from typing import Any

_COMMAND_CATEGORY_MAP: dict[str, str] = {
    "grep": "grep",
    "egrep": "grep",
    "fgrep": "grep",
    "rg": "grep",
    "find": "find",
    "fd": "find",
    "cat": "cat",
    "head": "head",
    "tail": "tail",
    "ls": "ls",
    "dir": "ls",
    "cd": "cd",
    "pwd": "pwd",
    "mkdir": "mkdir",
    "cp": "cp",
    "mv": "mv",
    "rm": "rm",
    "chmod": "chmod",
    "touch": "touch",
    "sed": "sed",
    "awk": "awk",
    "sort": "sort",
    "uniq": "uniq",
    "wc": "wc",
    "cut": "cut",
    "tee": "tee",
    "diff": "diff",
    "xargs": "xargs",
    "base64": "base64",
    "echo": "echo",
    "printf": "echo",
    "export": "export",
    "python": "python",
    "python3": "python",
    "python3.12": "python",
    "python3.11": "python",
    "pytest": "pytest",
    "django": "pytest",
    "pip": "pip",
    "pip3": "pip",
    "rscript": "r",
    "r": "r",
    "spark-submit": "spark",
    "pyspark": "spark",
    "jupyter": "jupyter",
    "node": "node",
    "npm": "npm",
    "npx": "npm",
    "yarn": "npm",
    "pnpm": "npm",
    "git": "git",
    "curl": "curl",
    "wget": "curl",
    "apt": "apt",
    "apt-get": "apt",
    "conda": "conda",
    "docker": "docker",
    "podman": "docker",
    "systemctl": "systemctl",
    "ps": "ps",
    "kill": "kill",
    "df": "df",
    "du": "df",
    "free": "free",
    "mount": "mount",
    "make": "make",
    "cmake": "make",
    "ninja": "make",
    "gcc": "gcc",
    "g++": "gcc",
    "clang": "gcc",
    "clang++": "gcc",
    "sqlite3": "sqlite3",
    "duckdb": "duckdb",
    "psql": "psql",
    "mysql": "mysql",
    "tar": "tar",
    "gzip": "tar",
    "zip": "tar",
    "unzip": "tar",
    "sha256sum": "checksum",
    "md5sum": "checksum",
    "true": "true",
    "false": "true",
    "sleep": "sleep",
    "date": "date",
    "time": "time",
    "man": "man",
    "bash": "bash",
    "sh": "bash",
}

_COMMAND_PRIORITY: dict[str, int] = {
    **{name: 4 for name in ("pytest", "pip", "pip3", "spark-submit")},
    **{
        name: 3
        for name in (
            "python",
            "python3",
            "python3.12",
            "python3.11",
            "git",
            "docker",
            "podman",
            "make",
            "cmake",
            "ninja",
            "gcc",
            "g++",
            "clang",
            "clang++",
            "apt",
            "apt-get",
            "conda",
            "npm",
            "npx",
            "curl",
            "wget",
            "rscript",
            "spark-submit",
            "jupyter",
            "sqlite3",
            "duckdb",
            "psql",
        )
    },
    **{
        name: 2
        for name in (
            "grep",
            "rg",
            "find",
            "fd",
            "sed",
            "awk",
            "diff",
            "cat",
            "tar",
            "chmod",
            "cp",
            "mv",
            "rm",
            "mkdir",
            "touch",
            "ps",
            "df",
            "du",
            "free",
            "sha256sum",
            "base64",
        )
    },
}

_SAFE_EXECUTABLE_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_ENV_ASSIGN_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_SHELL_RESERVED_WORDS = frozenset({"if", "then", "else", "fi", "for", "while", "do", "done"})
_WRAPPERS_WITH_OPTION_VALUE = {
    "sudo": {"-u", "--user", "-g", "--group"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
}
_WRAPPERS = frozenset({"sudo", "nice", "nohup", "timeout"})
_PYTHON_INTERPRETERS = frozenset({"python", "python3", "python3.12", "python3.11"})


def classify_exec_tool_name(tool_name: str, tool_args: str | dict[str, Any] | None) -> str:
    if tool_name != "exec":
        return tool_name
    command = extract_command(tool_args)
    if not command:
        return tool_name
    base = extract_base_command(command)
    if base == "exec":
        return tool_name
    category = _COMMAND_CATEGORY_MAP.get(base.lower()) or _safe_unknown_category(base)
    return f"exec-{category}" if category else tool_name


def extract_exec_operation(tool_name: str, tool_args: Any) -> str | None:
    if tool_name.startswith("exec-") and len(tool_name) > len("exec-"):
        return tool_name.split("-", 1)[1]
    classified = classify_exec_tool_name(tool_name, tool_args)
    if classified.startswith("exec-"):
        return classified.split("-", 1)[1]
    return None


def extract_operation(request: Any) -> str | None:
    operation_hint = getattr(request, "operation_hint", None)
    if operation_hint:
        return operation_hint
    return extract_exec_operation(
        getattr(request, "tool_name", "unknown"),
        getattr(request, "raw_params", None),
    )


def extract_command(tool_args: str | dict[str, Any] | None) -> str | None:
    if isinstance(tool_args, str):
        try:
            parsed = json.loads(tool_args)
        except (json.JSONDecodeError, TypeError):
            return None
        return extract_command(parsed)
    if not isinstance(tool_args, dict):
        return None
    value = tool_args.get("command") or tool_args.get("cmd")
    if isinstance(value, str):
        return value
    nested = tool_args.get("exec")
    if isinstance(nested, dict):
        nested_value = nested.get("command") or nested.get("cmd")
        if isinstance(nested_value, str):
            return nested_value
    return None


def extract_base_command(command: str) -> str:
    if not isinstance(command, str) or not command:
        return "exec"
    best = "exec"
    best_priority = -1
    for segment in _split_shell_segments(command):
        token = _tokenize_segment(segment)
        if not token:
            continue
        priority = _COMMAND_PRIORITY.get(token.lower(), 1)
        if priority >= best_priority:
            best = token
            best_priority = priority
    return best


def _split_shell_segments(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        char = command[i]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single and (i == 0 or command[i - 1] != "\\"):
            in_double = not in_double
        if not in_single and not in_double:
            if char == "|" and (i == 0 or command[i - 1] != "\\"):
                segments.append("".join(current))
                current = []
                i += 1
                continue
            if char == ";" or (char == "&" and i + 1 < len(command) and command[i + 1] == "&"):
                segments.append("".join(current))
                current = []
                i += 2 if char == "&" else 1
                continue
        current.append(char)
        i += 1
    segments.append("".join(current))
    return segments


def _tokenize_segment(segment: str) -> str:
    try:
        parts = shlex.split(segment.strip(), posix=True)
    except ValueError:
        return ""
    if not parts:
        return ""
    idx = _skip_assignments(parts, 0)
    idx = _unwrap_wrappers(parts, idx)
    if idx >= len(parts):
        return ""
    token = _basename(parts[idx])
    if token in _SHELL_RESERVED_WORDS:
        return ""
    if token == "command":
        idx += 1
        while idx < len(parts) and parts[idx].startswith("-"):
            if "v" in parts[idx][1:] or "V" in parts[idx][1:]:
                return "exec"
            idx += 1
        return _basename(parts[idx]) if idx < len(parts) else "command"
    if token == "xargs":
        command_idx = _xargs_command_index(parts, idx + 1)
        if command_idx is not None:
            token = _basename(parts[command_idx])
            idx = command_idx
    if token in _PYTHON_INTERPRETERS and len(parts) > idx + 2 and parts[idx + 1] == "-m":
        module = parts[idx + 2]
        if module in _COMMAND_CATEGORY_MAP:
            token = module
    if token in {"cd", "pushd", "popd", "pwd", "ls", "dir"}:
        for candidate in parts[idx + 1 :]:
            base = _basename(candidate)
            if _COMMAND_PRIORITY.get(base, -1) >= 3:
                token = base
    return token


def _unwrap_wrappers(parts: list[str], idx: int) -> int:
    while idx < len(parts):
        wrapper = _basename(parts[idx])
        if wrapper not in _WRAPPERS:
            return idx
        idx += 1
        while idx < len(parts):
            option = parts[idx]
            option_name = option.split("=", 1)[0]
            if wrapper == "nice" and re.fullmatch(r"-\d+", option):
                idx += 1
                continue
            if option_name in _WRAPPERS_WITH_OPTION_VALUE.get(wrapper, set()):
                idx += 1 if "=" in option else 2
                continue
            if option.startswith("-"):
                idx += 1
                continue
            if wrapper == "timeout":
                idx += 1
            break
        idx = _skip_assignments(parts, idx)
    return idx


def _xargs_command_index(parts: list[str], idx: int) -> int | None:
    value_options = {"-a", "--arg-file", "-I", "-n", "-P", "--max-procs"}
    flag_options = {"-0", "--null", "-r", "--no-run-if-empty"}
    while idx < len(parts):
        option = parts[idx]
        option_name = option.split("=", 1)[0]
        if option_name in value_options:
            idx += 1 if "=" in option or len(option) > 2 and option.startswith("-n") else 2
            continue
        if option in flag_options:
            idx += 1
            continue
        if option.startswith("-"):
            return None
        return idx
    return None


def _skip_assignments(parts: list[str], idx: int) -> int:
    while idx < len(parts) and _ENV_ASSIGN_TOKEN_RE.fullmatch(parts[idx]):
        idx += 1
    return idx


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _safe_unknown_category(token: str) -> str | None:
    basename = _basename(token).lower()
    if not basename or basename in _SHELL_RESERVED_WORDS or len(basename) > 64:
        return None
    return basename if _SAFE_EXECUTABLE_RE.fullmatch(basename) else None
