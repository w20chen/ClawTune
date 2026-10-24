from __future__ import annotations

import json
from functools import lru_cache

from jsonschema import Draft202012Validator
from clawtune_kb.contracts import data_root


@lru_cache(maxsize=8)
def schema(name: str) -> dict:
    # Wheel copies are generated from contracts/ by the packaging build.
    path = data_root() / "contracts" / (name + ".schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def validator(name: str) -> Draft202012Validator:
    value = schema(name)
    Draft202012Validator.check_schema(value)
    return Draft202012Validator(value)


def validate(name: str, value: dict) -> None:
    validator(name).validate(value)


def validate_rule(rule: dict) -> None:
    if len(json.dumps(rule).encode()) > 256 * 1024:
        raise ValueError("rule too large")
    validate("command-rule", rule)

    def visit(grammar: dict, depth: int = 0) -> None:
        if depth > 16:
            raise ValueError("grammar too deep")
        names: set[str] = set()
        numeric_short = 0
        for opt in grammar.get("options", []):
            if names.intersection(opt["names"]) or opt["canonical"] not in opt["names"]:
                raise ValueError("duplicate option alias or unevidenced canonical name")
            names.update(opt["names"])
            if opt["max"] is not None and opt["min"] > opt["max"]:
                raise ValueError("invalid option arity")
            if opt.get("leading_numeric_short"):
                numeric_short += 1
                if numeric_short > 1 or opt["min"] != 1 or opt["max"] != 1 or opt.get("transition"):
                    raise ValueError("numeric shorthand requires one unambiguous single-value option")
        positions = grammar.get("positionals", [])
        for i, slot in enumerate(positions):
            if slot["max"] is None and i != len(positions) - 1:
                raise ValueError("only the last positional may be variadic")
        subnames: set[str] = set()
        for sub in grammar.get("subcommands", []):
            if subnames.intersection(sub["names"]):
                raise ValueError("duplicate subcommand")
            subnames.update(sub["names"])
            visit(sub["grammar"], depth + 1)
    visit(rule["grammar"])
