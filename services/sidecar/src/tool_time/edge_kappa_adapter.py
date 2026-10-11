"""Adapt the shared command feature contract to EdgeKappaKB."""
from __future__ import annotations

import shlex

from command_parser import ParseContext, default_service
from command_parser.models import FORMAT, CommandFeatures
from edge_kappa_kb import FeatureQuery

NORMALIZATION_VERSION = FORMAT

def feature_query(record: CommandFeatures) -> FeatureQuery:
    return FeatureQuery(record.features, record.core, record.tool, FORMAT)

def shell_query(command: str, *, repo=None, cwd=None, env_id=None, is_clause=True) -> FeatureQuery:
    if is_clause:
        return feature_query(default_service().argv(shlex.split(command), ParseContext(repo=repo,cwd=cwd,env_id=env_id)))
    from command_parser.shell import clauses
    row = clauses(command)[0]
    return feature_query(default_service().argv(row["argv"], ParseContext(repo=repo,cwd=cwd,env_id=env_id), word_intents=row.get("word_intents")))
