from __future__ import annotations

from pathlib import PurePosixPath

from .models import ParseContext, ParsedCommand, script_identity
from .registry import RuleRegistry


class CommandParser:
    def __init__(self, registry: RuleRegistry) -> None:
        self.registry = registry

    def parse(self, argv: list[str], context: ParseContext, *, word_intents: list | None = None, script: bool = False) -> ParsedCommand:
        if not argv:
            raise ValueError("empty command")
        root, _ = self.registry.lookup(argv[0], context, script=script)
        result = ParsedCommand(root["canonical"] if root else PurePosixPath(argv[0]).name, list(argv))
        if len(argv) > 16384:
            result.status = "partial"
            result.diagnostics.append("word_limit")
            return result
        unresolved = set()
        for i, word in enumerate(word_intents or []):
            if any(c.get("kind") not in {"literal"} for c in word.get("components", [])):
                unresolved.add(i)

        def add_role(layer, kind, start, end):
            result.roles.append({"scope": layer["scope"], "kind": kind, "start": start, "end": end})

        def opaque(layer: dict, values: list[str], reason: str, *, invalid: bool = False) -> None:
            layer["opaque"].extend(values)
            if values:
                add_role(layer, "opaque", len(argv) - len(values), len(argv))
            result.diagnostics.append(reason)
            result.status = "invalid" if invalid or result.status == "invalid" else "partial"

        def invoke(tokens: list[str], offset: int, *, script: bool = False) -> None:
            rule, key = self.registry.lookup(tokens[0], context, script=script)
            name = rule["canonical"] if rule else tokens[0]
            if script:
                name = script_identity(tokens[0], context)
            layer = {"scope": f"L{len(result.layers)}/{name}", "entry": tokens[0], "mode": "default",
                     "options": [], "args": [], "opaque": [], "payload": None, "script": script}
            result.layers.append(layer)
            add_role(layer, "entry", offset, offset + 1)
            if len(result.layers) > 8:
                opaque(layer, tokens[1:], "layer_limit")
                return
            if offset in unresolved:
                opaque(layer, tokens, "unresolved_executable")
                return
            if rule is None:
                opaque(layer, tokens[1:], "missing_rule:" + tokens[0])
                return
            result.rules.append(key)
            grammar = rule["grammar"]
            seen: set[str] = set()
            i, pos, position, consumed = 1, 0, 0, 0
            ended = False

            def transition(action: dict, value: str, next_index: int) -> bool:
                layer["mode"] = action["mode"]
                kind = action["action"]
                if kind == "opaque":
                    if action["mode"] == "inline":
                        layer["payload"] = value
                        add_role(layer, "payload", offset + next_index - 1, offset + next_index)
                    # User argv after an interpreter boundary are literal operands.
                    layer["args"].extend(tokens[next_index:])
                    if next_index < len(tokens):
                        add_role(layer, "operand", offset + next_index, offset + len(tokens))
                    return True
                result.target.append(script_identity(value, context) if kind == "script" else value)
                invoke([value, *tokens[next_index:]], offset + next_index - 1, script=kind == "script")
                return True

            while i < len(tokens):
                token = tokens[i]
                if offset + i in unresolved:
                    opaque(layer, tokens[i:], "unresolved_expansion")
                    return
                syntax = grammar.get("syntax", {})
                options = {alias: option for option in grammar.get("options", []) for alias in option["names"]}
                literal = grammar.get("literals", {}).get(token)
                if not ended and literal:
                    transition(literal, token, i + 1)
                    return
                if not ended and token == "--":
                    ended = True
                    i += 1
                    continue
                matched: list[tuple[dict, str | None]] = []
                if not ended and token in options:
                    matched = [(options[token], None)]
                elif not ended and token.startswith("--") and "=" in token:
                    spelling, value = token.split("=", 1)
                    if spelling in options and "equals" in options[spelling]["forms"]:
                        matched = [(options[spelling], value)]
                elif not ended and token.startswith("-") and not token.startswith("--") and len(token) > 2:
                    tail = token[1:]
                    while tail:
                        spelling = "-" + tail[0]
                        if spelling not in options:
                            matched = []
                            break
                        opt = options[spelling]
                        if opt["max"] != 0:
                            if len(tail) > 1 and "attached" not in opt["forms"]:
                                matched = []
                                break
                            matched.append((opt, tail[1:] or None))
                            break
                        if not syntax.get("clusters", False):
                            matched = []
                            break
                        matched.append((opt, None))
                        tail = tail[1:]
                if matched:
                    next_i = i + 1
                    for option, attached in matched:
                        values = [] if attached is None else [attached]
                        limit = option["max"]
                        if attached is not None and limit == 0:
                            opaque(layer, tokens[i:], "unexpected_option_value", invalid=True)
                            return
                        while (limit is None or len(values) < limit) and next_i < len(tokens):
                            if "separate" not in option["forms"]:
                                break
                            if len(values) >= option["min"] and option["optional_value"] == "attached":
                                break
                            value = tokens[next_i]
                            if option["optional_value"] == "numeric" and len(values) >= option["min"] and not _number(value):
                                break
                            if offset + next_i in unresolved:
                                opaque(layer, tokens[i:], "unresolved_option_value")
                                return
                            if (value.startswith("-") and value != "-" and option["dash_values"] != "any"
                                    and not (option["dash_values"] == "numeric" and _number(value))):
                                break
                            if value in options and len(values) >= option["min"]:
                                break
                            values.append(value)
                            next_i += 1
                        if len(values) < option["min"]:
                            opaque(layer, tokens[i:], "missing_option_value", invalid=True)
                            return
                        seen.update(option["names"])
                        add_role(layer, "entry_option" if option.get("transition") else "option", offset + i, offset + next_i)
                        if option.get("transition"):
                            if not values:
                                opaque(layer, tokens[i:], "missing_entry", invalid=True)
                                return
                            transition(option["transition"], values[0], next_i)
                            return
                        layer["options"].append({"name": option["canonical"], "values": values,
                                                  "repeat": option["repeat"], "independent": option.get("independent", False)})
                    i = next_i
                    continue
                if not ended and token.startswith("-") and token != "-":
                    opaque(layer, tokens[i:], "unknown_option:" + token)
                    return
                sub = next((s for s in grammar.get("subcommands", []) if token in s["names"]), None)
                if not ended and sub and pos == 0:
                    add_role(layer, "subcommand", offset + i, offset + i + 1)
                    result.target.append(sub["canonical"])
                    parent = [o for o in grammar.get("options", []) if o.get("persistent")]
                    grammar = {**sub["grammar"], "options": [*parent, *sub["grammar"].get("options", [])]}
                    i += 1
                    consumed = 0
                    continue
                if syntax.get("assignments") and "=" in token and token.split("=", 1)[0].isidentifier():
                    layer["args"].append(token)
                    i += 1
                    continue
                slots = grammar.get("positionals", [])
                while pos < len(slots) and seen.intersection(slots[pos].get("skip_if_options", [])):
                    pos += 1
                if pos >= len(slots):
                    opaque(layer, tokens[i:], "unmodelled_operand")
                    return
                slot = slots[pos]
                role = slot["role"]
                if role in {"script_entry", "module_entry", "command_tail"}:
                    action = {"script_entry": "script", "module_entry": "module", "command_tail": "command"}[role]
                    transition({"action": action, "mode": action}, token, i + 1)
                    return
                if role == "opaque_payload":
                    add_role(layer, "payload", offset + i, offset + len(tokens))
                    layer["payload"] = "\0".join(tokens[i:])
                    if grammar.get("partial"):
                        opaque(layer, [], "partial_grammar")
                    return
                layer["args"].append(token)
                add_role(layer, "operand", offset + i, offset + i + 1)
                consumed += 1
                position += 1
                if slot["max"] is not None and consumed >= slot["max"]:
                    pos += 1
                    consumed = 0
                if not syntax.get("intermixed", True):
                    ended = True
                i += 1
            # Completion schemas often omit requiredness. Check only explicit minima.
            for slot_index, slot in enumerate(grammar.get("positionals", [])):
                if seen.intersection(slot.get("skip_if_options", [])):
                    continue
                if slot_index >= pos and slot["min"] > (consumed if slot_index == pos else 0):
                    opaque(layer, [], "missing_operand", invalid=True)
                    break
        invoke(list(argv), 0, script=script or root is None and "/" in argv[0])
        return result


def _number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False
