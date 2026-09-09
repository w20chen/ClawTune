from __future__ import annotations

import argparse
import json
from pathlib import Path

from cold_start.manifest import build_manifest, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Reproducible per-repository task split and train-only cold KB export")
    parser.add_argument("command", choices=("split", "export"))
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rss-unit", choices=("MB", "MiB"))
    parser.add_argument("--trust-call-cgroup", action="store_true")
    args = parser.parse_args()
    if args.manifest.resolve().is_relative_to(args.dataset.resolve()):
        parser.error("manifest must be outside the read-only dataset")
    if args.command == "split":
        if args.manifest.exists():
            parser.error("manifest already exists; reuse it or choose a new versioned path")
        result = build_manifest(args.dataset, seed=args.seed)
        write_json(args.manifest, result)
        print(json.dumps({"train": len(result["train_tasks"]), "test": len(result["test_tasks"]),
                          "repos": len(result["repositories"]), "manifest_sha256": result["manifest_sha256"]}))
    else:
        if args.output is None or args.rss_unit is None:
            parser.error("export requires --output and an explicit --rss-unit")
        from cold_start.export import export
        result = export(args.dataset, json.loads(args.manifest.read_text(encoding="utf-8")), args.output,
                        rss_unit=args.rss_unit, trust_call_cgroup=args.trust_call_cgroup)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
