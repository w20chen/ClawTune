from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

SCHEMA = "clawtune_task_split.v1"
RULE = "sha256_rank_per_repo_v1"


def task_identity(name: str) -> tuple[str, str]:
    """Accept [corpus__]owner__repository-PR[.trace[.jsonl]]."""
    stem = Path(name).name
    for suffix in (".jsonl", ".trace"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    parts = stem.split("__")
    if len(parts) not in (2, 3):
        raise ValueError(f"invalid task filename: {name}")
    owner, instance = parts[-2:]
    match = re.fullmatch(r"(.+)-(\d+)", instance)
    if not owner or not match or any(c in owner + instance for c in ("/", "\\")):
        raise ValueError(f"invalid task filename: {name}")
    return f"{owner}/{match[1]}", f"{owner}__{instance}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def build_manifest(dataset: Path, *, seed: int = 42) -> dict:
    dataset = dataset.resolve(strict=True)
    files = sorted(p for p in dataset.iterdir() if p.is_file() and
                   (p.name.endswith(".trace.jsonl") or p.name.endswith(".trace")))
    if not files:
        raise ValueError("no flat trace task files found")
    tasks = {}
    for path in files:
        if not path.resolve().is_relative_to(dataset):
            raise ValueError("dataset file resolves outside the dataset")
        repo, task = task_identity(path.name)
        entry = tasks.setdefault(task, {"repo": repo, "files": []})
        entry["files"].append({"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    groups = {}
    for task, entry in tasks.items():
        groups.setdefault(entry["repo"], []).append(task)
    train, test, repo_counts = [], [], {}
    for repo, members in sorted(groups.items()):
        members.sort(key=lambda task: (hashlib.sha256(
            json.dumps([RULE, seed, repo, task], separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(), task))
        n = 1 if len(members) == 1 else max(1, math.floor(len(members) * .8))
        train.extend(members[:n])
        test.extend(members[n:])
        repo_counts[repo] = {"total": len(members), "train": n, "test": len(members) - n}
    result = {"schema": SCHEMA, "rule": RULE, "seed": seed, "train_fraction": .8,
              "rounding": "floor_80_percent_singletons_train", "tasks": dict(sorted(tasks.items())),
              "repositories": repo_counts, "train_tasks": sorted(train), "test_tasks": sorted(test)}
    result["manifest_sha256"] = fingerprint(result)
    return result


def validate_manifest(manifest: dict, dataset: Path, *, verify_files: bool = True) -> None:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("schema") != SCHEMA or fingerprint(unsigned) != manifest.get("manifest_sha256"):
        raise ValueError("split manifest is invalid or was modified")
    train, test = set(manifest["train_tasks"]), set(manifest["test_tasks"])
    if train & test or train | test != set(manifest["tasks"]):
        raise ValueError("split tasks overlap or are incomplete")
    for task, entry in manifest["tasks"].items():
        for file in entry["files"]:
            path = (dataset / file["path"]).resolve(strict=True)
            if not path.is_relative_to(dataset.resolve()) or task_identity(path.name) != (entry["repo"], task):
                raise ValueError("manifest task identity/path mismatch")
            if verify_files and (path.stat().st_size != file["bytes"] or sha256_file(path) != file["sha256"]):
                raise ValueError(f"dataset changed: {file['path']}")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
