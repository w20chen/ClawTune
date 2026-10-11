from __future__ import annotations

from collections import defaultdict

from .models import CommandFeatures, ParseContext, ParsedCommand, canonical_json, digest


def atom(kind: str, *values) -> str:
    return kind + ":" + canonical_json(list(values))


def _value(value):
    return {"sha256": digest(["argument", value])} if len(canonical_json(value)) > 4096 else value


def project(parsed: ParsedCommand, context: ParseContext, *, max_features: int = 20) -> CommandFeatures:
    core = {"tool=" + parsed.tool}
    if parsed.target:
        core.add(atom("target", *parsed.target))
    preferred, optional = set(), set()
    for index, layer in enumerate(parsed.layers):
        scope = layer["scope"]
        if layer["mode"] != "default":
            preferred.add(atom("mode", layer["mode"]) if index == 0 else atom("mode", scope, layer["mode"]))
        by_name = defaultdict(list)
        ordered = []
        for option in layer["options"]:
            if option["independent"]:
                by_name[option["name"]].append(option)
            else:
                ordered.append([option["name"], option["values"]])
        if ordered:
            optional.add(atom("sequence", scope, _value(ordered)))
        for name, occurrences in by_name.items():
            policy = occurrences[-1]["repeat"]
            values = [o["values"] for o in occurrences]
            if policy == "count":
                optional.add(atom("count", scope, name, len(values)))
            elif policy == "last" or len(values) == 1:
                optional.add(atom("opt", scope, name, _value(values[-1])) if values[-1] else atom("flag", scope, name))
            else:
                if policy == "set":
                    values = [list(v) for v in sorted({tuple(v) for v in values})]
                optional.add(atom("repeat", scope, name, _value(values)))
        for position, value in enumerate(layer["args"]):
            optional.add(atom("arg", scope, position, _value(value)))
        if layer["opaque"]:
            optional.add(atom("opaque", scope, _value(layer["opaque"])))
        if layer["payload"] is not None:
            optional.add(atom("payload", scope, digest(["payload", layer["payload"]])))
    for key in ("repo", "cwd", "env_id"):
        if value := getattr(context, key):
            preferred.add(atom("context", key, value))
    ordered = sorted(core) + sorted(preferred - core) + sorted(optional - core - preferred)
    limit = max(len(core), max_features)
    return CommandFeatures(parsed.tool, frozenset(ordered[:limit]), frozenset(core),
                           digest(parsed.to_dict()), parsed.status, max(0, len(ordered) - limit))
