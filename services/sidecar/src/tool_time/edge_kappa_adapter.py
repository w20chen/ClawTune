"""Versioned shell feature adapter for the independent EdgeKappaKB."""

from __future__ import annotations

from edge_kappa_kb import FeatureQuery
from tool_time._lattice_vendor.normalize import normalize_command

NORMALIZATION_VERSION = "shell-normalize-v1"


def shell_query(command: str, *, repo: str | None = None, cwd: str | None = None,
                env_id: str | None = None, is_clause: bool = True) -> FeatureQuery:
    features, core = normalize_command(command, repo=repo, cwd=cwd, env_id=env_id,
                                       is_clause=is_clause)
    tools = [feature[5:] for feature in features if feature.startswith("tool=")]
    if len(tools) != 1:
        raise ValueError("shell command must have exactly one normalized tool")
    return FeatureQuery(features, core, tools[0], NORMALIZATION_VERSION)
