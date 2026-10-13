"""Classify exec shell commands by extracting the primary sub-command.

When a shell command uses ``&&``, ``||``, ``;``, or ``|`` to chain multiple
sub-commands, this module identifies the highest-priority sub-command for
time prediction modeling.

The core insight: in compound commands like ``cd /x && python setup.py build``,
the ``python`` sub-command dominates execution time, not ``cd``.
"""

from __future__ import annotations

import re
import shlex

# ── Command category map ────────────────────────────────────────────────────
_COMMAND_CATEGORY_MAP: dict[str, str] = {
    "grep": "grep", "egrep": "grep", "fgrep": "grep", "rg": "grep",
    "find": "find", "fd": "find", "locate": "find",
    "which": "which", "whereis": "which", "type": "which",
    "cat": "cat", "head": "head", "tail": "tail", "less": "less", "more": "less",
    "ls": "ls", "dir": "ls", "cd": "cd", "pushd": "cd", "popd": "cd", "pwd": "pwd",
    "mkdir": "mkdir", "cp": "cp", "mv": "mv", "rm": "rm", "rmdir": "rm",
    "chmod": "chmod", "chown": "chmod", "touch": "touch", "ln": "ln",
    "sed": "sed", "awk": "awk", "sort": "sort", "uniq": "uniq", "wc": "wc",
    "tr": "tr", "cut": "cut", "tee": "tee", "diff": "diff", "patch": "diff",
    "xargs": "xargs", "base64": "base64",
    "echo": "echo", "printf": "echo", "source": "source", "export": "export",
    "env": "env", "unset": "env", "set": "env",
    "python": "python", "python3": "python", "pip": "pip", "pip3": "pip",
    "pytest": "pytest", "django": "pytest",
    "python3.12": "python", "python3.11": "python",
    "python3.10": "python", "python3.9": "python",
    "R": "r", "Rscript": "r",
    "scala": "scala", "scalac": "scala", "sbt": "sbt",
    "spark-submit": "spark", "spark-shell": "spark", "pyspark": "spark",
    "node": "node", "npm": "npm", "npx": "npm", "yarn": "npm", "pnpm": "npm",
    "git": "git",
    "curl": "curl", "wget": "curl",
    "jupyter": "jupyter",
    "apt": "apt", "apt-get": "apt", "apt-cache": "apt",
    "yum": "apt", "dnf": "apt", "apk": "apt", "brew": "apt",
    "conda": "conda", "mamba": "conda",
    "docker": "docker", "podman": "docker",
    "systemctl": "systemctl", "service": "systemctl",
    "ps": "ps", "kill": "kill", "killall": "kill",
    "top": "top", "htop": "top",
    "df": "df", "du": "df", "free": "free",
    "mount": "mount", "umount": "mount",
    "make": "make", "cmake": "make", "ninja": "make",
    "gcc": "gcc", "g++": "gcc", "clang": "gcc", "clang++": "gcc",
    "sqlite3": "sqlite3", "duckdb": "duckdb",
    "psql": "psql", "mysql": "mysql",
    "mariadb": "mariadb", "mongosh": "mongosh", "redis-cli": "redis-cli",
    "tar": "tar", "gzip": "tar", "gunzip": "tar", "zip": "tar", "unzip": "tar",
    "xxd": "xxd", "md5sum": "checksum", "sha1sum": "checksum",
    "sha256sum": "checksum", "sha512sum": "checksum",
    "file": "file",
    "true": "true", "false": "true", "test": "test",
    "sleep": "sleep", "date": "date", "time": "time",
    "watch": "watch", "man": "man", "info": "man",
    "su": "su", "sudo": "su",
    "bash": "bash", "sh": "bash", "zsh": "bash",
}

# ── Priority tiers for disambiguating compound commands ─────────────────────
_COMMAND_PRIORITY: dict[str, int] = {
    "pip": 4, "pip3": 4, "pytest": 4, "django": 4,
    "python": 3, "python3": 3, "python3.12": 3, "python3.11": 3,
    "python3.10": 3, "python3.9": 3,
    "git": 3, "docker": 3, "podman": 3,
    "make": 3, "cmake": 3, "ninja": 3,
    "gcc": 3, "g++": 3, "clang": 3, "clang++": 3,
    "apt": 3, "apt-get": 3, "yum": 3, "dnf": 3, "apk": 3, "brew": 3,
    "conda": 3, "mamba": 3,
    "npm": 3, "npx": 3, "yarn": 3, "pnpm": 3, "node": 3,
    "systemctl": 3, "service": 3,
    "curl": 3, "wget": 3,
    "su": 3, "sudo": 3,
    "R": 3, "Rscript": 3, "scala": 3, "scalac": 3, "sbt": 3,
    "spark-submit": 4, "spark-shell": 4, "pyspark": 4,
    "jupyter": 3,
    "sqlite3": 3, "duckdb": 3, "psql": 3, "mysql": 3,
    "mariadb": 3, "mongosh": 3, "redis-cli": 3,
    "grep": 2, "egrep": 2, "fgrep": 2, "rg": 2,
    "find": 2, "fd": 2, "sed": 2, "awk": 2, "diff": 2, "patch": 2,
    "cat": 2, "tar": 2, "gzip": 2, "gunzip": 2, "zip": 2, "unzip": 2,
    "chmod": 2, "chown": 2, "cp": 2, "mv": 2, "rm": 2, "rmdir": 2,
    "mkdir": 2, "touch": 2, "ln": 2, "kill": 2, "killall": 2,
    "mount": 2, "umount": 2, "ps": 2, "top": 2, "htop": 2,
    "df": 2, "du": 2, "free": 2, "which": 2, "whereis": 2,
    "man": 2, "watch": 2, "xxd": 2, "md5sum": 2,
    "sha1sum": 2, "sha256sum": 2, "sha512sum": 2, "file": 2, "base64": 2,
    "xargs": 1, "head": 1, "tail": 1, "less": 1, "more": 1,
    "sort": 1, "uniq": 1, "wc": 1, "cd": 1, "pushd": 1, "popd": 1,
    "ls": 1, "dir": 1, "pwd": 1, "echo": 1, "printf": 1,
    "true": 1, "false": 1, "test": 1, "sleep": 1, "date": 1, "time": 1,
    "source": 1, "export": 1, "env": 1, "unset": 1, "set": 1,
    "bash": 1, "sh": 1, "zsh": 1, "tee": 1, "cut": 1, "tr": 1,
    "locate": 1, "type": 1, "info": 1,
}

_SAFE_EXECUTABLE_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_MAX_EXECUTABLE_SLUG_LENGTH = 64
_ENV_ASSIGN_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_SHELL_RESERVED_WORDS = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do",
    "done", "case", "esac", "in", "function", "select", "coproc",
})

_WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-C", "--close-from", "-D", "--chdir", "-g", "--group",
        "-h", "--host", "-p", "--prompt", "-R", "--chroot", "-r", "--role",
        "-t", "--type", "-u", "--user"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata"}),
    "chroot": frozenset({"--userspec", "--groups"}),
    "flock": frozenset({"-w", "--wait", "-E", "--conflict-exit-code"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
}
_WRAPPER_FLAG_OPTIONS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-A", "-b", "-E", "-H", "-i", "--login", "-K", "-k",
        "-n", "-P", "-S", "-s", "--shell"}),
    "nice": frozenset(), "nohup": frozenset(),
    "ionice": frozenset({"-t", "--ignore"}),
    "chroot": frozenset({"--skip-chdir"}),
    "flock": frozenset({"-s", "--shared", "-x", "--exclusive", "-u", "--unlock",
        "-n", "--nonblock", "-o", "--close", "-F", "--no-fork", "--verbose"}),
    "stdbuf": frozenset(),
    "timeout": frozenset({"--preserve-status", "--foreground", "-v", "--verbose"}),
}


def _executable_basename(token: str) -> str:
    """Return a POSIX executable basename without accessing the filesystem."""
    return token.rsplit("/", 1)[-1]


def _safe_unknown_category(token: str) -> str | None:
    """Return a bounded category slug for an unknown executable token."""
    basename = _executable_basename(token).lower()
    if basename in _SHELL_RESERVED_WORDS:
        return None
    if not basename or len(basename) > _MAX_EXECUTABLE_SLUG_LENGTH:
        return None
    if _SAFE_EXECUTABLE_RE.fullmatch(basename) is None:
        return None
    return basename


def _unwrap_command_wrappers(parts: list[str], start: int) -> int:
    """Return the command index after supported wrappers."""
    idx = start
    while idx < len(parts) and _ENV_ASSIGN_TOKEN_RE.fullmatch(parts[idx]):
        idx += 1
    while idx < len(parts):
        wrapper_idx = idx
        wrapper = _executable_basename(parts[idx])
        if wrapper not in _WRAPPER_VALUE_OPTIONS:
            return idx
        option_start = idx + 1
        if wrapper == "nice" and option_start < len(parts) and re.fullmatch(r"-\d+", parts[option_start]) is not None:
            option_start += 1
        consumed = _consume_wrapper_options(parts, option_start, wrapper)
        if consumed is None:
            return wrapper_idx
        idx, non_executing = consumed
        if non_executing:
            return wrapper_idx
        if idx >= len(parts):
            return wrapper_idx
        while idx < len(parts) and _ENV_ASSIGN_TOKEN_RE.fullmatch(parts[idx]):
            idx += 1
    return idx


def _consume_wrapper_options(parts: list[str], start: int, wrapper: str) -> tuple[int, bool] | None:
    """Consume wrapper options. Returns (next_idx, non_executing) or None."""
    value_opts = _WRAPPER_VALUE_OPTIONS.get(wrapper, frozenset())
    flag_opts = _WRAPPER_FLAG_OPTIONS.get(wrapper, frozenset())
    idx = start
    while idx < len(parts):
        option = parts[idx]
        if option == "--":
            return idx + 1, False
        if option == "-" or not option.startswith("-"):
            return idx, False
        option_name = option.split("=", 1)[0]
        if option_name in value_opts:
            if "=" in option:
                idx += 1
            elif len(option_name) == 2 and len(option) > 2:
                idx += 1
            else:
                if idx + 1 >= len(parts):
                    return None
                idx += 2
        elif option in flag_opts:
            idx += 1
        elif len(option) > 2 and option.startswith("-") and not option.startswith("--"):
            short_value = {o for o in value_opts if len(o) == 2}
            if any(option.startswith(o) for o in short_value):
                idx += 1
            elif all(f"-{c}" in flag_opts for c in option[1:]):
                idx += 1
            else:
                return None
        else:
            return idx, False
    return idx, False


def _tokenize_segment(segment: str) -> str:
    """Extract the base command token from a single shell segment."""
    seg = segment.strip()
    if not seg:
        return ""
    try:
        parts = shlex.split(seg, posix=True)
    except ValueError:
        return ""
    if not parts:
        return ""

    _NAVIGATION_TOKENS = frozenset({"cd", "pushd", "popd", "pwd", "ls", "dir"})
    token_idx = _unwrap_command_wrappers(parts, 0)
    if token_idx >= len(parts):
        return ""
    token = _executable_basename(parts[token_idx])

    # Handle ``command`` builtin
    if token == "command" and len(parts) > 1:
        token_idx += 1
        while token_idx < len(parts) and parts[token_idx].startswith("-"):
            option = parts[token_idx]
            if "v" in option[1:] or "V" in option[1:]:
                return "exec"
            if option not in {"-p", "--"}:
                return "command"
            token_idx += 1
        if token_idx == len(parts):
            return "command"
        token = _executable_basename(parts[token_idx])

    # Recovery for navigation-prefixed traces
    if token in _NAVIGATION_TOKENS:
        best_action_token = ""
        best_action_priority = -1
        for idx, raw_token in enumerate(parts[token_idx + 1:], token_idx + 1):
            candidate = _executable_basename(raw_token)
            priority = _COMMAND_PRIORITY.get(candidate, -1)
            if priority > best_action_priority:
                best_action_token = candidate
                best_action_priority = priority
        if best_action_priority >= 3:
            token = best_action_token

    # ``python -m <module>`` redirect
    _PYTHON_INTERPS = frozenset({"python", "python3", "python3.9", "python3.10",
                                  "python3.11", "python3.12"})
    if token in _PYTHON_INTERPS and token_idx >= 0:
        if len(parts) > token_idx + 2 and parts[token_idx + 1] == "-m":
            module_token = parts[token_idx + 2]
            if module_token in _COMMAND_CATEGORY_MAP:
                token = module_token

    # xargs plumbing
    if token == "xargs" and token_idx >= 0:
        if token_idx + 1 < len(parts):
            token = _executable_basename(parts[token_idx + 1])

    return token


def extract_primary_command(shell_cmd: str) -> str:
    """Extract the primary (highest-priority) sub-command from a shell command.

    Splits on ``&&``, ``||``, ``;``, and ``|``, then picks the segment
    with the highest priority base command.

    Args:
        shell_cmd: A shell command string, possibly compound.

    Returns:
        The primary base command token (e.g., ``python``, ``pytest``, ``make``).
        Returns ``"exec"`` if no command can be identified.
    """
    if not shell_cmd or not isinstance(shell_cmd, str):
        return "exec"

    # Split into segments respecting shell quoting
    segments: list[str] = []
    current = ""
    i = 0
    in_single = False
    in_double = False
    while i < len(shell_cmd):
        ch = shell_cmd[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            if i == 0 or shell_cmd[i - 1] != "\\":
                in_double = not in_double
        if not in_single and not in_double:
            if ch == "|" and (i == 0 or shell_cmd[i - 1] != "\\"):
                segments.append(current)
                current = ""
                i += 1
                continue
            if ch == "&" and i + 1 < len(shell_cmd) and shell_cmd[i + 1] == "&":
                segments.append(current)
                current = ""
                i += 2
                continue
            if ch == ";":
                segments.append(current)
                current = ""
                i += 1
                continue
        current += ch
        i += 1
    segments.append(current)

    best_token = "exec"
    best_priority = -1
    for seg in segments:
        token = _tokenize_segment(seg)
        if not token:
            continue
        prio = _COMMAND_PRIORITY.get(token, 1)
        if prio >= best_priority:
            best_priority = prio
            best_token = token

    return best_token


def classify_exec(cmd: str) -> str:
    """Classify an exec shell command, returning the category slug.

    Args:
        cmd: The shell command string.

    Returns:
        A category slug like ``python``, ``pytest``, ``make``, etc.
    """
    base = extract_primary_command(cmd)
    if base == "exec":
        return base
    category = _COMMAND_CATEGORY_MAP.get(base)
    if category is None:
        category = _safe_unknown_category(base)
    return category if category else "exec"
