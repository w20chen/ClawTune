from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

FORMAT = "command-features.v1"
FeatureSet = frozenset[str]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class ParseContext:
    repo: str | None = None
    cwd: str | None = None
    runtime_id: str | None = None
    workspace: str | None = None
    host_workspace: str | None = None
    container_id: str | None = None
    image: str | None = None
    env_id: str | None = None
    tool_version: str | None = None


def script_identity(entry: str, context: ParseContext) -> str:
    if not context.workspace:
        return posixpath.normpath(entry)
    if context.container_id:
        path = posixpath.normpath(posixpath.join(context.cwd or context.workspace, entry))
        relative = posixpath.relpath(path, context.workspace)
        return relative if relative != ".." and not relative.startswith("../") else path
    root = Path(context.workspace).resolve()
    path = (root / (context.cwd or ".") / entry).resolve()
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


@dataclass
class ParsedCommand:
    tool: str
    argv: list[str]
    layers: list[dict] = field(default_factory=list)
    target: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    roles: list[dict] = field(default_factory=list)
    status: str = "complete"
    format: str = "parsed-command.v1"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CommandFeatures:
    tool: str
    features: FeatureSet
    core: FeatureSet
    parse_digest: str
    status: str = "complete"
    omitted_features: int = 0
    format: str = FORMAT

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", frozenset(self.features))
        object.__setattr__(self, "core", frozenset(self.core))
        if self.format != FORMAT or not self.core <= self.features:
            raise ValueError("invalid command feature format/core")
        if {f for f in self.features if f.startswith("tool=")} != {"tool=" + self.tool}:
            raise ValueError("command features require exactly one tool")
        if "tool=" + self.tool not in self.core:
            raise ValueError("tool must be core")

    def to_dict(self) -> dict:
        return {**asdict(self), "features": sorted(self.features), "core": sorted(self.core)}

    @classmethod
    def from_dict(cls, value: dict) -> CommandFeatures:
        return _decode_features(canonical_json(value))


@lru_cache(maxsize=16384)
def _decode_features(text: str) -> CommandFeatures:
    from .validation import validate
    value = json.loads(text)
    validate("command-features", value)
    return CommandFeatures(**value)
