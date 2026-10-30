from __future__ import annotations

import threading
from collections import Counter
from dataclasses import replace
from functools import lru_cache

from .models import CommandFeatures, ParseContext
from .parser import CommandParser
from .projection import project
from .registry import RuleRegistry


class CommandFeatureService:
    def __init__(self, registry: RuleRegistry | None = None, generator=None) -> None:
        self.registry = registry or RuleRegistry()
        self.parser = CommandParser(self.registry)
        self.generator = generator
        self._calls: dict[str, dict[int, tuple[tuple[str, ...], dict]]] = {}
        self._lock = threading.RLock()
        self._discovered = set()
        self._metrics = Counter()

    def argv(self, argv, context: ParseContext | None = None, *, word_intents=None, max_features=20) -> CommandFeatures:
        context = context or ParseContext()
        parsed = self.parser.parse(list(argv), context, word_intents=word_intents)
        if self.generator is not None:
            for layer in parsed.layers:
                if ("missing_rule:" + layer["entry"] in parsed.diagnostics or
                        layer is parsed.layers[-1] and any(d.startswith(("unknown_option:", "partial_grammar")) for d in parsed.diagnostics)):
                    self.generator.enqueue(layer["entry"], context, script=layer["script"], argv=list(argv))
        features = project(parsed, context, max_features=max_features)
        with self._lock:
            self._metrics[parsed.status] += 1
            self._metrics["feature_truncated"] += int(features.omitted_features > 0)
        return features

    def metrics(self, *, reset=False):
        with self._lock:
            result = dict(self._metrics)
            if reset:
                self._metrics.clear()
            return result

    def prepare(self, clauses, context: ParseContext, call_id: str | None = None) -> tuple[dict, ...]:
        result = []
        with self._lock:
            bound = self._calls.setdefault(call_id, {}) if call_id else {}
            for index, raw in enumerate(clauses):
                clause = dict(raw)
                identity = int(clause.get("clause_index", index))
                argv = tuple(clause.get("syntax_argv", clause["argv"]))
                old = bound.get(identity)
                if old and old[0] != argv:
                    raise ValueError("call clause identity reused with different argv")
                clause_context = context
                if clause.get("cwd"):
                    import posixpath
                    cwd = clause["cwd"]
                    # Only a literal, success-dependent cd establishes the new cwd.
                    if "conditional_on_previous_success" in clause.get("prediction_assumptions", []) and not any(c in cwd for c in "$`~*"):
                        clause_context = replace(context, cwd=posixpath.normpath(posixpath.join(context.cwd or context.workspace or "", cwd)))
                    else:
                        clause_context = replace(context, cwd=None, workspace=None)
                features = old[1] if old else self.argv(argv, clause_context, word_intents=clause.get("word_intents")).to_dict()
                bound[identity] = (argv, features)
                clause["command_features"] = features
                clause["clause_index"] = identity
                result.append(clause)
        return tuple(result)

    def bind_observations(self, observations, call_id: str | None, *, release: bool = False):
        with self._lock:
            bound = self._calls.pop(call_id, {}) if release else self._calls.get(call_id, {})
            output = []
            normalized = {}
            for row in observations:
                from tool_resource.commands import unwrap_argv
                candidates = (bound.items() if row.clause_index is None else
                              ((row.clause_index, bound[row.clause_index]),)
                              if row.clause_index in bound else ())
                matches = []
                for index, value in candidates:
                    if value[0] == row.argv:
                        matches.append((index, value))
                        continue
                    if index not in normalized:
                        normalized[index] = tuple(unwrap_argv(value[0])[0])
                    if normalized[index] == row.argv:
                        matches.append((index, value))
                if len(matches) == 1:
                    index, (_, features) = matches[0]
                    output.append(replace(row, command_features=features, clause_index=index))
                else:
                    output.append(row)
            return output

    def discover(self, context: ParseContext) -> None:
        identity = (context.repo, context.runtime_id, context.workspace)
        with self._lock:
            if self.generator is None or identity in self._discovered:
                return
            self._discovered.add(identity)
        self.generator.discover(context)

    def close(self, timeout_seconds=None) -> None:
        if self.generator:
            self.generator.close(timeout_seconds=timeout_seconds)
        with self._lock:
            self._calls.clear()


@lru_cache(maxsize=1)
def default_service() -> CommandFeatureService:
    """Immutable bundled-only service for standalone/offline callers."""
    return CommandFeatureService()


def observation_features(row) -> CommandFeatures:
    if getattr(row, "command_features", None) is not None:
        return CommandFeatures.from_dict(row.command_features)
    return default_service().argv(row.argv, ParseContext(repo=row.repo))


def clause_features(clause, repo: str | None = None) -> CommandFeatures:
    if clause.get("command_features") is not None:
        return CommandFeatures.from_dict(clause["command_features"])
    return default_service().argv(clause["argv"], ParseContext(repo=repo), word_intents=clause.get("word_intents"))
