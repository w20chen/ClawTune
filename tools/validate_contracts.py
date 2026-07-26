from __future__ import annotations

import json
from pathlib import Path

from copy import deepcopy

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


EXAMPLES = {
    "tool-before-request.schema.json": "tool-before-request.json",
    "tool-decision.schema.json": "tool-decision.json",
    "tool-completed-event.schema.json": "tool-completed-event.json",
    "model-event.schema.json": "model-event.json",
    "execution-registration.schema.json": "execution-registration.json",
    "execution-claim.schema.json": "execution-claim.json",
    "execution-started.schema.json": "execution-started.json",
    "execution-exited.schema.json": "execution-exited.json",
}


def main() -> None:
    store = {}
    for path in CONTRACTS.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        store[path.name] = schema
        if "$id" in schema:
            store[schema["$id"]] = schema
    for schema_name, example_name in EXAMPLES.items():
        schema = inline_local_refs(store[schema_name], store)
        example = json.loads((CONTRACTS / "examples" / example_name).read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(example)
        print(f"validated {example_name} against {schema_name}")


def inline_local_refs(value: object, store: dict[str, object]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            ref = value["$ref"]
            if isinstance(ref, str) and ref in store:
                return inline_local_refs(deepcopy(store[ref]), store)
        return {key: inline_local_refs(child, store) for key, child in value.items()}
    if isinstance(value, list):
        return [inline_local_refs(item, store) for item in value]
    return value


if __name__ == "__main__":
    main()
