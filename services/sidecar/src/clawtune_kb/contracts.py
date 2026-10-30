from pathlib import Path
import json
from functools import lru_cache

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


@lru_cache(maxsize=4)
def _schemas(root: Path) -> tuple[dict, Registry]:
    resources = []
    schemas = {}
    for path in root.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[path.name] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return schemas, Registry().with_resources(resources)


@lru_cache(maxsize=128)
def _validator(root: Path, name: str) -> Draft202012Validator:
    schemas, registry = _schemas(root)
    return Draft202012Validator(schemas[name], registry=registry)


def validate(value: dict, name: str) -> None:
    # Installed contracts are immutable for this process. Separate checkout and
    # wheel roots so one cannot accidentally use the other's protocol registry.
    _validator((data_root() / "contracts").resolve(), name).validate(value)
