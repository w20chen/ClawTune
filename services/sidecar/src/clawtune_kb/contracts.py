from pathlib import Path
import json

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


def data_root() -> Path:
    module = Path(__file__).resolve()
    parents = module.parents
    if len(parents) > 4 and [parent.name for parent in parents[:4]] == [
        "clawtune_kb", "src", "sidecar", "services"
    ]:
        checkout = parents[4]
        if (checkout / "contracts/kb-seed.schema.json").is_file():
            return checkout
    return module.parent / "_data"


def validate(value: dict, name: str) -> None:
    root = data_root() / "contracts"
    resources = []
    for path in root.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    schema = json.loads((root / name).read_text(encoding="utf-8"))
    Draft202012Validator(schema, registry=Registry().with_resources(resources)).validate(value)
