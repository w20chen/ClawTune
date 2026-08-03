"""Command normalization and feature extraction.

Converts shell commands into feature sets for lattice-based matching.
Features are order-independent; ``python 1.py -a 1 -b 2`` and
``python 1.py -b 2 -a 1`` normalize to the same feature set.

For **clause-level** data (fine-grained trace with ``clause_telemetry``),
each observation is already a single sub-command; compound command
splitting (``&&``, ``||``, ``;``) is a fast no-op.

For **legacy** data (full compound commands in a single observation),
the highest-priority sub-command is extracted before feature normalization.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import FrozenSet, List, Tuple

FeatureSet = FrozenSet[str]

# Tools that are trivial pipe consumers: they only filter/transform
# an in-memory pipe stream and consume negligible CPU time.
# Their wall-clock latency in clause_telemetry is artificially inflated
# (equal to the producer's time) due to pipe concurrency semantics.
# We exclude them from both training and prediction.
_TRIVIAL_PIPE_TOOLS: frozenset[str] = frozenset({
    "tail", "head", "wc", "cat", "tee", "cut", "tr",
})


def is_trivial_pipe_tool(bin_name: str) -> bool:
    """Return True if *bin_name* is a trivial pipe consumer.

    These tools read from stdin via a pipe and perform O(n) trivial
    work.  Their wall-clock time is dominated by the upstream producer.
    """
    return _canonicalize_tool(bin_name) in _TRIVIAL_PIPE_TOOLS


def _canonicalize_tool(tool_name: str) -> str:
    """Map tool names to canonical forms for lattice deduplication.

    - All Python variants (python, python3, python3.10, /path/to/python)
      → ``"python"``
    - All pip variants (pip, pip3) → ``"pip"``

    The basename has already been extracted by the caller, so
    ``tool_name`` is the final path component without directories.
    """
    import re
    
    # Python: python, python3, python3.8, python3.10, python3.12, etc.
    if re.fullmatch(r'python3?(?:\.\d+)?', tool_name):
        return "python"
    
    # Pip: pip, pip3
    if tool_name in ("pip", "pip3"):
        return "pip"
    
    return tool_name


def _basename(token: str) -> str:
    """Extract a POSIX basename from a path-like token."""
    return PurePosixPath(token).name


def _split_compound_command(cmd: str) -> list[str]:
    """Split a compound shell command into logical segments.

    Splits on ``&&``, ``||``, ``;`` only — NOT on ``|`` (pipes are
    part of a single logical command chain, not independent segments).
    Respects shell quoting.
    """
    segments: list[str] = []
    current = ""
    i = 0
    in_single = False
    in_double = False
    while i < len(cmd):
        ch = cmd[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            if i == 0 or cmd[i - 1] != "\\":
                in_double = not in_double
        if not in_single and not in_double:
            if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
                segments.append(current)
                current = ""
                i += 2
                continue
            if ch == "|" and i + 1 < len(cmd) and cmd[i + 1] == "|":
                # || — logical OR, split
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
    return [s.strip() for s in segments if s.strip()]


def split_all_clauses(cmd: str) -> list[str]:
    """Split a compound shell command into all independent clauses.

    Unlike ``_split_compound_command`` (which only splits on ``&&``,
    ``||``, ``;``), this function **also** splits on pipe ``|`` so that
    each clause can be predicted independently against the lattice.

    Splits on: ``|``, ``||``, ``&&``, ``;``.
    Respects shell quoting.

    Example:
        ``grep -n pat file | head -40``
        → ``["grep -n pat file", "head -40"]``
    """
    segments: list[str] = []
    current = ""
    i = 0
    in_single = False
    in_double = False
    while i < len(cmd):
        ch = cmd[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            if i == 0 or cmd[i - 1] != "\\":
                in_double = not in_double
        if not in_single and not in_double:
            if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
                # &&
                segments.append(current)
                current = ""
                i += 2
                continue
            if ch == "|":
                if i + 1 < len(cmd) and cmd[i + 1] == "|":
                    # || — logical OR
                    segments.append(current)
                    current = ""
                    i += 2
                    continue
                else:
                    # | — pipe (single bar)
                    segments.append(current)
                    current = ""
                    i += 1
                    continue
            if ch == ";":
                segments.append(current)
                current = ""
                i += 1
                continue
        current += ch
        i += 1
    segments.append(current)
    return [s.strip() for s in segments if s.strip()]


def split_clauses_with_separators(cmd: str) -> "list[ClauseSegment]":
    """Split a compound command into clauses, preserving the separator type.

    Unlike ``split_all_clauses`` (which discards separator info), this
    function returns ``ClauseSegment`` objects that record which separator
    (``|``, ``&&``, ``||``, ``;``) follows each clause.  The last clause
    always has an empty separator.

    This enables **separator-aware time aggregation** at prediction time:

    - ``|`` → parallel execution → ``max(t1, t2)``
    - ``&&``, ``||``, ``;`` → serial execution → ``t1 + t2``

    Example:
        ``grep x | head -5 && wc -l``
        → ``[ClauseSegment("grep x", "|"), ClauseSegment("head -5", "&&"), ClauseSegment("wc -l", "")]``
    """
    from tool_time._lattice_vendor.schemas import ClauseSegment

    segments: list[ClauseSegment] = []
    current = ""
    i = 0
    in_single = False
    in_double = False
    clause_index = 0

    while i < len(cmd):
        ch = cmd[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            if i == 0 or cmd[i - 1] != "\\":
                in_double = not in_double
        if not in_single and not in_double:
            if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
                segments.append(ClauseSegment(
                    cmd=current.strip(), separator="&&", clause_index=clause_index,
                ))
                clause_index += 1
                current = ""
                i += 2
                continue
            if ch == "|":
                if i + 1 < len(cmd) and cmd[i + 1] == "|":
                    # || — logical OR
                    segments.append(ClauseSegment(
                        cmd=current.strip(), separator="||", clause_index=clause_index,
                    ))
                    clause_index += 1
                    current = ""
                    i += 2
                    continue
                else:
                    # | — pipe
                    segments.append(ClauseSegment(
                        cmd=current.strip(), separator="|", clause_index=clause_index,
                    ))
                    clause_index += 1
                    current = ""
                    i += 1
                    continue
            if ch == ";":
                segments.append(ClauseSegment(
                    cmd=current.strip(), separator=";", clause_index=clause_index,
                ))
                clause_index += 1
                current = ""
                i += 1
                continue
        current += ch
        i += 1

    # Last clause: empty separator
    if current.strip() or not segments:
        segments.append(ClauseSegment(
            cmd=current.strip(), separator="", clause_index=clause_index,
        ))

    return [s for s in segments if s.cmd]


# Priority for tool categories (higher = more important)
_TOOL_PRIORITY: dict[str, int] = {
    "pytest": 4, "django": 4, "pip": 4, "pip3": 4,
    "python": 3, "python3": 3, "python3.12": 3, "python3.11": 3,
    "python3.10": 3, "python3.9": 3,
    "git": 3, "docker": 3, "make": 3, "cmake": 3, "ninja": 3,
    "gcc": 3, "g++": 3, "clang": 3, "clang++": 3,
    "apt": 3, "apt-get": 3, "conda": 3, "npm": 3, "npx": 3,
    "node": 3, "curl": 3, "wget": 3, "sudo": 3, "su": 3,
    "grep": 2, "rg": 2, "find": 2, "fd": 2, "sed": 2, "awk": 2,
    "cat": 2, "cp": 2, "mv": 2, "rm": 2, "mkdir": 2, "chmod": 2,
    "cd": 1, "ls": 1, "pwd": 1, "echo": 1, "sleep": 1, "date": 1,
    "export": 1, "env": 1, "source": 1, "bash": 1, "sh": 1,
}


def _get_segment_priority(segment: str) -> int:
    """Get priority of a command segment based on its primary tool.

    Uses exec_classifier's segment tokenizer for proper tool extraction,
    including navigation recovery (``cd /x && python ...`` → ``python``).
    """
    from tool_time._lattice_vendor.exec_classifier import _tokenize_segment
    token = _tokenize_segment(segment)
    if not token or token == "exec":
        return -1
    return _TOOL_PRIORITY.get(token, 0)


def extract_primary_segment(cmd: str) -> str:
    """Extract the primary sub-command from a possibly compound shell command.

    For compound commands (with ``&&``, ``||``, ``;``), returns
    the highest-priority segment.  Ties are broken by preferring
    the *rightmost* segment — the final action in the chain.
    Pipes (``|``) are NOT treated as segment separators; the
    entire pipe chain is one logical command.

    For simple commands, returns the original command unchanged.
    """
    # Quick check: no compound separators
    if not any(sep in cmd for sep in ("&&", "||", ";")):
        return cmd

    segments = _split_compound_command(cmd)
    if len(segments) <= 1:
        return cmd

    # Find highest-priority segment.  >= ensures rightmost-wins for ties.
    best_seg = segments[-1]  # default to rightmost
    best_prio = _get_segment_priority(best_seg)
    for seg in reversed(segments[:-1]):
        prio = _get_segment_priority(seg)
        if prio > best_prio:
            best_prio = prio
            best_seg = seg

    return best_seg


def _strip_heredoc(cmd: str) -> tuple[str, str | None]:
    """Remove heredoc content from a command string, returning the body.

    ``python3 << 'EOF' ... EOF`` becomes ``("python3", "\\n...\\n")``.
    The entire heredoc construct (``<<``, delimiter, body, closing
    delimiter) is removed from the command.  The body is returned
    separately so callers can extract semantic features from it
    (e.g. Python ``import`` statements).

    Returns:
        Tuple of ``(stripped_command, heredoc_body_or_None)``.
    """
    import re
    # Match << [-] 'WORD' or << [-] WORD
    heredoc_match = re.search(r"<<-?\s*(['\"])?(\w+)\1", cmd)
    if not heredoc_match:
        return cmd, None
    delimiter = heredoc_match.group(2)
    # Find the delimiter on its own line
    rest = cmd[heredoc_match.end():]
    # Find the closing delimiter (must be on its own line)
    closing_pattern = re.compile(rf"^{delimiter}\s*$", re.MULTILINE)
    closing = closing_pattern.search(rest)
    if closing:
        # Extract body (content between << DELIM and closing DELIM)
        body = rest[:closing.start()]
        # Keep only the part before << (exclude <<, delimiter, body)
        before = cmd[:heredoc_match.start()]
        after = rest[closing.end():] if closing.end() < len(rest) else ""
        return (before + " " + after).strip(), body
    return cmd, None


def _extract_heredoc_features(body: str) -> list[str]:
    """Extract key semantic features from a heredoc body.

    Currently extracts top-level Python import modules, which are strong
    indicators of script purpose and runtime cost (``import torch`` vs
    ``import os`` have very different timing profiles).

    Capped at 5 imports to prevent lattice node explosion from
    scripts with many imports (each import becomes an optional feature
    that generates subset nodes combinatorially).

    Example:
        ``import torch; from sklearn.ensemble import RF``
        → ``["heredoc_import=torch", "heredoc_import=sklearn"]``
    """
    import re
    MAX_IMPORTS = 5
    features: list[str] = []

    # import X, import X as Y, import X, Y, Z (comma-separated)
    for m in re.finditer(r'^\s*import\s+(.+?)(?:\s+#|$)', body, re.MULTILINE):
        modules_str = m.group(1).split('#')[0]  # strip inline comment
        # Split on commas, handle "X as Y" aliases
        for part in modules_str.split(','):
            part = part.strip()
            if not part:
                continue
            # "numpy as np" → "numpy"
            module = part.split()[0].split('.')[0]
            features.append(f'heredoc_import={module}')
            if len(features) >= MAX_IMPORTS:
                break
        if len(features) >= MAX_IMPORTS:
            break

    # from X import Y (X may be dotted: sklearn.ensemble)
    if len(features) < MAX_IMPORTS:
        for m in re.finditer(r'^\s*from\s+([\w.]+)\s+import', body, re.MULTILINE):
            module = m.group(1).split('.')[0]
            features.append(f'heredoc_import={module}')
            if len(features) >= MAX_IMPORTS:
                break

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for f in features:
        if f not in seen:
            seen.add(f)
            result.append(f)
    return result


def _filter_redirections(tokens: list[str]) -> list[str]:
    """Remove shell I/O redirection operators and their file targets.

    Shell redirections (``>``, ``>>``, ``<``, ``2>``, ``2>&1``,
    ``&>``, ``>/dev/null``, etc.) are irrelevant for command timing
    prediction — they are handled by the shell before the command runs.

    Operates on already-tokenized input so that ``shlex`` quoting is
    respected (e.g. ``echo ">"`` keeps the literal ``>``).
    """
    import re
    result: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]

        # ── Self-contained fd redirect: 2>&1, 1>&2, 0<&3, etc. ──────
        if re.match(r'^\d*[<>]&\d+$', token):
            i += 1
            continue

        # ── Redirect-to-file operators: N>, N>>, N<, &>, >&, etc. ──
        # These consume the next token as the filename target.
        if (re.match(r'^(\d+)?>>?$', token)       # >, >>, 2>, 2>>
                or re.match(r'^(\d+)?<$', token)   # <, 0<
                or re.match(r'^&>>?$', token)      # &>, &>>
                or re.match(r'^>>?&$', token)):     # >&, >>&
            i += 1  # skip the redirect operator
            # Also skip the filename target (if present and not another option)
            if i < n and not tokens[i].startswith('-'):
                i += 1
            continue

        # ── Standalone <, <<, <<< (when shlex separates them) ────────
        if token in ('<', '<<', '<<<'):
            i += 1  # skip operator
            if i < n:
                i += 1  # skip target word / heredoc marker
            continue

        # ── No-space redirect: >file (token starts with >) ──────────
        if re.match(r'^>>?\S', token):
            i += 1
            continue

        result.append(token)
        i += 1

    return result


def normalize_command(
    cmd: str,
    *,
    repo: str | None = None,
    cwd: str | None = None,
    env_id: str | None = None,
    max_features: int = 20,
    is_clause: bool = False,
) -> Tuple[FeatureSet, FeatureSet]:
    """Normalize a shell command into core and full feature sets.

    By default, compound commands are first split and the primary
    sub-command is extracted before feature normalization.
    Set ``is_clause=True`` to skip this extraction (the caller has
    already split the compound command into individual clauses).

    Heredoc content and long inline scripts are truncated to prevent
    feature explosion.

    Args:
        cmd: The shell command string.
        repo: Optional repository identifier (e.g., ``owner/repo``).
        cwd: Optional working directory.
        env_id: Optional environment identifier.
        max_features: Maximum number of total features to retain.
            Beyond this limit, further positional args are dropped.
        is_clause: If True, skip primary-sub-command extraction
            (the command is already a single clause).
    """
    # Extract primary sub-command from compound commands (unless already a clause)
    if not is_clause:
        cmd = extract_primary_segment(cmd)

    # Strip heredoc content (return body separately for feature extraction)
    cmd, heredoc_body = _strip_heredoc(cmd)

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()

    # Strip shell I/O redirections — irrelevant for timing prediction
    tokens = _filter_redirections(tokens)

    if not tokens:
        raise ValueError("Command cannot be empty.")

    features: List[str] = []
    core: List[str] = []

    # ── Heredoc semantic features (optional, not core) ──────────────────
    # Extract import-level features from heredoc body so that different
    # scripts produce different lattice nodes (e.g. import-torch vs import-os).
    if heredoc_body:
        heredoc_features = _extract_heredoc_features(heredoc_body)
        features.extend(heredoc_features)

    # ── Context features (optional, not core) ───────────────────────────
    # repo, cwd, and env_id are optional features — they refine the
    # command identity but don't define it.  This design allows:
    #   1. Cross-repo nodes: {tool=make, flag:-j=8}  (no repo)
    #   2. Repo-specific nodes: {repo=A, tool=make, flag:-j=8}
    # Both coexist in the lattice.  Risk-aware dominance selects the
    # best one for the query.
    if repo:
        features.append(f"repo={repo}")
    if cwd:
        features.append(f"cwd={cwd}")
    if env_id:
        features.append(f"env_id={env_id}")

    # ── Tool ─────────────────────────────────────────────────────────────
    tool = _canonicalize_tool(_basename(tokens[0]))
    tool_feat = f"tool={tool}"
    features.append(tool_feat)
    core.append(tool_feat)

    i = 1

    # ── Primary target / script / subcommand ─────────────────────────────
    # For most tools, the first non-option token is the primary target.
    # Special case: ``-m module`` (e.g. ``python -m pytest``) — the
    # module name is the effective target.
    if i < len(tokens):
        if tokens[i] == "-m" and i + 1 < len(tokens):
            # python -m module → module is the effective target
            target_feat = f"target={tokens[i + 1]}"
            features.append(target_feat)
            core.append(target_feat)
            features.append(f"opt:-m={tokens[i + 1]}")
            i += 2
        elif not tokens[i].startswith("-"):
            target_feat = f"target={tokens[i]}"
            features.append(target_feat)
            core.append(target_feat)
            i += 1

    # ── Remaining tokens: options, flags, positional args ────────────────
    import re
    pos_idx = 0
    while i < len(tokens) and len(features) < max_features:
        token = tokens[i]

        # --key=value form
        if token.startswith("-") and "=" in token:
            name, value = token.split("=", 1)
            features.append(f"opt:{name}={value}")
            i += 1
            continue

        # Combined short flags: -rn → flag:-r, flag:-n (never takes a value).
        # Only split very short tokens (2-3 letters) to avoid false
        # positives on single-dash long options like -slow, -fast, etc.
        if re.match(r'^-[a-zA-Z]{2,3}$', token):
            for ch in token[1:]:
                features.append(f"flag:-{ch}")
            i += 1
            continue

        # -k value or --key value form
        if token.startswith("-"):
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                features.append(f"opt:{token}={tokens[i + 1]}")
                i += 2
                continue
            # Boolean flag: -q, --verbose, etc.
            features.append(f"flag:{token}")
            i += 1
            continue

        # Positional argument
        features.append(f"arg{pos_idx}={token}")
        pos_idx += 1
        i += 1

    return frozenset(features), frozenset(core)


def feature_to_string(features: FrozenSet[str]) -> str:
    """Format a feature set as a readable string."""
    return "{" + ", ".join(sorted(features)) + "}"


def features_match_exact(
    query_features: FrozenSet[str],
    node_features: FrozenSet[str],
) -> bool:
    """Check if query features exactly match node features."""
    return query_features == node_features
