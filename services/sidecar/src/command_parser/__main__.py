"""Inspect the same rules and feature projection used by the sidecar."""
import argparse
import json
from pathlib import Path

from .models import ParseContext
from .parser import CommandParser
from .projection import project
from .registry import RuleRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, help="sidecar command-rules cache directory")
    parser.add_argument("--repo")
    parser.add_argument("--runtime-id")
    parser.add_argument("--workspace")
    parser.add_argument("--cwd")
    parser.add_argument("argv", nargs=argparse.REMAINDER, help="-- EXECUTABLE ARG...")
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        parser.error("supply -- EXECUTABLE ARG...")
    context = ParseContext(repo=args.repo, runtime_id=args.runtime_id, workspace=args.workspace, cwd=args.cwd)
    parsed = CommandParser(RuleRegistry(args.cache)).parse(argv, context)
    print(json.dumps({"parsed": parsed.to_dict(), "features": project(parsed, context).to_dict()},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
