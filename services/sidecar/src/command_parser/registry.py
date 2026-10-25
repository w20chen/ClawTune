from __future__ import annotations

import copy
import json
import re
import threading
from pathlib import Path, PurePosixPath

from .models import ParseContext, digest
from .validation import validate_rule


class RuleRegistry:
    def __init__(self, cache_dir: Path | None = None, *, bundled: bool = True,
                 read_only: bool = False) -> None:
        self.cache_dir = cache_dir
        self.read_only = False
        self._lock = threading.RLock()
        self._rules: dict[str, dict] = {}
        self.generation = 0
        if bundled:
            for path in sorted((Path(__file__).parent / "rules").glob("*.json")):
                if path.name != "manifest.json":
                    self.add(json.loads(path.read_text(encoding="utf-8")), persist=False)
        if cache_dir and cache_dir.is_dir():
            for path in sorted(cache_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name)):
                try:
                    self.add(json.loads(path.read_text(encoding="utf-8")), persist=False)
                except Exception as exc:  # noqa: BLE001 - quarantine malformed external cache entries
                    # A bad cache entry never makes the bundled registry unavailable.
                    import logging
                    logging.getLogger(__name__).warning("invalid command rule %s: %s", path.name, type(exc).__name__)
        self.read_only = read_only

    def add(self, rule: dict, *, persist: bool = True) -> str:
        if self.read_only:
            raise RuntimeError("frozen command rule registry is read-only")
        validate_rule(rule)
        value = copy.deepcopy(rule)
        key = digest(value)
        with self._lock:
            self._rules[key] = value
            self.generation += 1
            if persist and self.cache_dir:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path = self.cache_dir / (key + ".json")
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temp.replace(path)
        return key

    def lookup(self, executable: str, context: ParseContext, *, script: bool = False) -> tuple[dict | None, str | None]:
        value, key = self._lookup_for_parse(executable, context, script=script)
        return copy.deepcopy(value), key

    def _lookup_for_parse(self, executable: str, context: ParseContext, *, script: bool = False) -> tuple[dict | None, str | None]:
        """Borrow a stable rule for the parser, which must only read it.

        add() owns a deep copy and replaces entries instead of mutating them.
        Public lookup() still returns an independent editable copy. The parser
        builds its own result containers and never exposes rule containers.
        """
        name = PurePosixPath(executable).name
        # Versioned Python names are declared family aliases, not arbitrary prefix matches.
        if re.fullmatch(r"python(?:[23](?:\.\d+)?)?", name):
            name = "python"
        found = []
        with self._lock:
            for order, (key, rule) in enumerate(self._rules.items()):
                scope = rule["scope"]
                if scope["kind"] == "global":
                    if script or name not in rule["names"]:
                        continue
                    priority = 0
                else:
                    if scope.get("runtime_id") != context.runtime_id or scope.get("repo") != context.repo:
                        continue
                    if scope.get("entry") != executable:
                        continue
                    if scope["kind"] == "script":
                        if not script:
                            continue
                        from .evidence import source as read_source
                        source = read_source(executable, context)
                        if not source or source["content_hash"] != scope.get("source_hash"):
                            continue
                    priority = 2 if scope["kind"] == "script" else 1
                if scope.get("tool_version") and scope["tool_version"] != context.tool_version:
                    continue
                found.append((priority, order, key, rule))
        if not found:
            return None, None
        _, _, key, value = max(found, key=lambda item: (item[0], item[1]))
        return value, key
