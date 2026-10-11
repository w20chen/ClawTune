from __future__ import annotations

import hashlib
import time
from pathlib import Path, PurePosixPath

import httpx

from .models import ParseContext

LIMIT = 65536


def record(kind: str, entry: str, text: str, source: str) -> dict:
    return {"kind": kind, "entry": entry, "text": text, "source": source,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "acquired_at": time.time(), "available": True}


def local_source(entry: str, context: ParseContext) -> dict | None:
    if context.container_id or not context.workspace:
        return None
    try:
        root = Path(context.workspace).resolve(strict=True)
        cwd = (root / (context.cwd or ".")).resolve(strict=True)
        path = (cwd / entry).resolve(strict=True)
        if not cwd.is_relative_to(root) or not path.is_relative_to(root) or not path.is_file():
            return None
        if path.stat().st_size > LIMIT:
            return None
        data = path.read_bytes()
        return record("source", entry, data.decode("utf-8"), str(path))
    except (OSError, UnicodeError, ValueError):
        return None


def docker_source(entry: str, context: ParseContext) -> dict | None:
    """Read a single bounded file through Docker's archive endpoint, without exec."""
    if not context.container_id or not context.workspace:
        return None
    path = PurePosixPath(context.cwd or context.workspace) / entry
    if ".." in path.parts or not path.is_relative_to(PurePosixPath(context.workspace)):
        return None
    import io
    import tarfile
    try:
        transport = httpx.HTTPTransport(uds="/var/run/docker.sock")
        with (httpx.Client(transport=transport, base_url="http://docker", timeout=3) as client,
              client.stream("GET", f"/containers/{context.container_id}/archive", params={"path": str(path)}) as response):
            response.raise_for_status()
            chunks = bytearray()
            for data in response.iter_bytes():
                chunks.extend(data)
                if len(chunks) > LIMIT + 16384:
                    return None
        with tarfile.open(fileobj=io.BytesIO(chunks)) as archive:
            members = archive.getmembers()
            if len(members) != 1 or not members[0].isfile() or members[0].size > LIMIT:
                return None
            stream = archive.extractfile(members[0])
            if stream is None:
                return None
            return record("source", entry, stream.read(LIMIT).decode("utf-8"), f"docker:{context.container_id}:{path}")
    except (OSError, ValueError, UnicodeError, httpx.HTTPError, tarfile.TarError):
        return None


def source(entry: str, context: ParseContext) -> dict | None:
    primary = docker_source(entry, context) if context.container_id else local_source(entry, context)
    if primary is None or context.container_id or not entry.endswith(".py"):
        return primary
    # Read a bounded set of direct local imports. Their text participates in
    # the content identity, so changing an argument-parser helper invalidates a rule.
    import ast
    try:
        tree = ast.parse(primary["text"])
    except SyntaxError:
        return primary
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level <= 1:
            modules.add(node.module)
    text = primary["text"]
    for module in sorted(modules)[:16]:
        candidate = str(Path(entry).parent / (module.replace(".", "/") + ".py"))
        helper = local_source(candidate, context)
        if helper and helper["source"] != primary["source"]:
            extra = "\n# Direct local dependency: " + candidate + "\n" + helper["text"]
            if len((text + extra).encode()) <= LIMIT:
                text += extra
    return record("source", entry, text, primary["source"])


def discover(context: ParseContext, limit: int = 10) -> list[str]:
    """Bounded local entry discovery; no recursive repository scan."""
    if context.container_id or not context.workspace:
        return []
    root = Path(context.workspace)
    candidates = {*root.glob("*.py"), *(root / "scripts").glob("*.py")}
    import json
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    for filename in ("pyproject.toml", "package.json"):
        item = local_source(filename, ParseContext(workspace=str(root), cwd=str(root)))
        if not item:
            continue
        try:
            if filename.endswith("toml"):
                obj = tomllib.loads(item["text"])
                for entry in obj.get("project", {}).get("scripts", {}).values():
                    module = entry.split(":", 1)[0].replace(".", "/") + ".py"
                    candidates.update((root / module, root / "src" / module))
            else:
                obj = json.loads(item["text"])
                entries = obj.get("bin", {})
                for entry in (entries.values() if isinstance(entries, dict) else [entries]):
                    if isinstance(entry, str):
                        candidates.add(root / entry)
        except (ValueError, TypeError, AttributeError):
            continue
    return [str(p.relative_to(root)) for p in sorted(candidates)
            if p.is_file() and p.resolve().is_relative_to(root.resolve())][:limit]
