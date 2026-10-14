"""Legacy file operations executed only inside the replay task's Docker sandbox.

Compatible with agent-test-bench's read/write/edit/list tool behavior, including
line pagination, line-trimmed edit matching, replace_all and CRLF preservation.
This module has no dependencies beyond the task image's Python standard library.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


IGNORED = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
           ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".coverage", "htmlcov"}


def execute(name: str, args: dict) -> str:
    try:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("Unknown path")
        target = Path(path).expanduser()
        if name == "read_file":
            lines = target.read_text(encoding="utf-8").splitlines()
            if not lines:
                return f"(Empty file: {path})"
            offset = max(1, args.get("offset", 1))
            if offset > len(lines):
                return f"Error: offset {offset} is beyond end of file ({len(lines)} lines)"
            end = min(len(lines), offset - 1 + (args.get("limit") or 2000))
            result, size = [], 0
            for index in range(offset - 1, end):
                line = f"{index + 1}| {lines[index]}"
                size += len(line) + 1
                if size > 128000:
                    break
                result.append(line)
            end = offset - 1 + len(result)
            footer = (f"Showing lines {offset}-{end} of {len(lines)}. Use offset={end + 1} to continue."
                      if end < len(lines) else f"End of file — {len(lines)} lines total")
            return "\n".join(result) + f"\n\n({footer})"
        if name == "write_file":
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("Unknown content")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path}"
        if name == "edit_file":
            old, new = args.get("old_text"), args.get("new_text")
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError("Unknown old_text or new_text")
            raw = target.read_bytes()
            content = raw.decode("utf-8").replace("\r\n", "\n")
            old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
            match, count = old, content.count(old)
            if not count:
                needle, lines = old.splitlines(), content.splitlines()
                matches = ["\n".join(lines[i:i + len(needle)])
                           for i in range(len(lines) - len(needle) + 1)
                           if needle and [line.strip() for line in lines[i:i + len(needle)]]
                           == [line.strip() for line in needle]]
                match, count = (matches[0], len(matches)) if matches else (None, 0)
            if match is None or not count:
                return f"Error: old_text not found in {path}"
            if count > 1 and not args.get("replace_all", False):
                return f"Warning: old_text appears {count} times. Provide more context or set replace_all=true."
            content = content.replace(match, new, -1 if args.get("replace_all", False) else 1)
            if b"\r\n" in raw:
                content = content.replace("\n", "\r\n")
            target.write_bytes(content.encode("utf-8"))
            return f"Successfully edited {path}"
        if name == "list_dir":
            if not target.is_dir():
                return f"Error: Not a directory: {path}"
            entries = []
            for root, directories, files in os.walk(target):
                directories[:] = sorted(d for d in directories if d not in IGNORED)
                for entry in directories + sorted(files):
                    relative = (Path(root) / entry).relative_to(target).as_posix()
                    entries.append(relative + ("/" if entry in directories else ""))
                if not args.get("recursive", False):
                    break
            cap = args.get("max_entries") or 200
            return "\n".join(entries[:cap]) + (f"\n... ({len(entries) - cap} more entries)" if len(entries) > cap else "")
        raise ValueError(f"unsupported replay file tool: {name}")
    except (OSError, ValueError, TypeError) as exc:
        return f"Error: {exc}"


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(execute(request["name"], request["arguments"]), ensure_ascii=True))
