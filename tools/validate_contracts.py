from __future__ import annotations

import json
from pathlib import Path

from copy import deepcopy

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


EXAMPLES = {
    "trace-event.schema.json": "trace-event.json",
    "runtime-abort.schema.json": "runtime-abort.json",
    "runtime-abort-response.schema.json": "runtime-abort-response.json",
    "benchmark-run.schema.json": "benchmark-run.json",
    "call-load.schema.json": "call-load.json",
    "pmu-prediction.schema.json": "pmu-prediction.json",
    "health.schema.json": "health.json",
    "clause-telemetry.schema.json": "clause-telemetry.json",
    "tool-before-request.schema.json": "tool-before-request.json",
    "tool-decision.schema.json": "tool-decision.json",
    "tool-completed-event.schema.json": "tool-completed-event.json",
    "model-event.schema.json": "model-event.json",
    "execution-registration.schema.json": "execution-registration.json",
    "execution-claim.schema.json": "execution-claim.json",
    "execution-started.schema.json": "execution-started.json",
    "execution-exited.schema.json": "execution-exited.json",
    "pmu-profile.schema.json": "pmu-profile.json",
}

TRACE_FIELD_REFERENCE_SCHEMAS = (
    "trace-event.schema.json",
    "clause-telemetry.schema.json",
    "tool-resource-observation.schema.json",
)


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
    for schema_name in TRACE_FIELD_REFERENCE_SCHEMAS:
        missing = list(properties_without_descriptions(store[schema_name]))
        if missing:
            raise ValueError(f"{schema_name} has undocumented fields: {', '.join(missing)}")
        print(f"validated field descriptions in {schema_name}")


def properties_without_descriptions(value: object, path: str = "$"):
    if not isinstance(value, dict):
        return
    for name, child in value.get("properties", {}).items():
        field_path = f"{path}.{name}"
        if "description" not in child:
            yield field_path
        yield from properties_without_descriptions(child, field_path)
    for name, child in value.get("$defs", {}).items():
        yield from properties_without_descriptions(child, f"{path}.$defs.{name}")


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
