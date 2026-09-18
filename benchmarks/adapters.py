"""Normalization only: benchmark identity never comes from its executor."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .bootstrap import ROOT

NAMES = ("swe-rebench", "deep-research-bench", "swe-bench-verified", "bfcl", "terminal-bench")

# These are intentionally small, reviewable input rosters.  They make every
# adapter runnable from a fresh checkout while keeping full upstream datasets
# outside the source tree.  A caller can still replace any roster with
# ``--dataset``.
BUNDLED_DATASETS = {
    "swe-rebench": "swe_rebench/tasks.json",
    "deep-research-bench": "deep_research_bench/tasks.json",
    "swe-bench-verified": "benchmarks/defaults/swe-bench-verified/tasks.json",
    "bfcl": "benchmarks/defaults/bfcl/tasks.json",
    "terminal-bench": "benchmarks/defaults/terminal-bench/tasks.json",
}

# Keep benchmark-specific defaults separate from the generated
# ``configs/benchmark.yaml`` so a clean checkout remains self-contained.
BUNDLED_CONFIGS = {
    name: f"benchmarks/defaults/{name}/config.yaml"
    for name in NAMES
}


@dataclass
class Task:
    benchmark: str
    task_id: str
    group: str
    kind: str
    prompt: str
    image: str = ""
    repo: str = ""
    base_commit: str = ""
    payload: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.benchmark}:{self.task_id}"

    @property
    def directory_name(self) -> str:
        return hashlib.sha256(self.key.encode()).hexdigest()[:20]

    def to_dict(self) -> dict:
        return asdict(self)


def _id(row: dict) -> str:
    for key in ("instance_id", "task_id", "id"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    raise ValueError("task requires a nonempty instance_id/task_id/id")


def _repo_task(name: str, row: dict) -> Task:
    task_id = _id(row)
    repo = row.get("repo")
    if not repo and "__" in task_id:
        owner, tail = task_id.split("__", 1)
        repo = owner + "/" + tail.rsplit("-", 1)[0]
    if not repo:
        raise ValueError(f"{name}/{task_id}: repository is required")
    image = row.get("image") or row.get("image_name") or row.get("docker_image")
    if not image:
        if name == "swe-bench-verified":
            image = f"docker.io/swebench/sweb.eval.x86_64.{task_id.replace('__', '_1776_')}:latest".lower()
        else:
            raise ValueError(f"{task_id}: SWE-Rebench requires its dataset-provided docker_image")
    prompt = row.get("problem_statement")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{task_id}: problem_statement is required")
    return Task(name, task_id, str(repo), "repository", prompt, str(image), str(repo),
                str(row.get("base_commit") or ""))


def _research(row: dict) -> Task:
    prompt = row.get("problem_statement") or row.get("prompt") or row.get("question")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("research task requires a question/prompt")
    group = str(row.get("topic") or row.get("domain") or "dataset")
    return Task("deep-research-bench", _id(row), group, "research", prompt,
                payload={"reference_answer": row.get("reference_answer") or row.get("article") or ""})


def _bfcl(row: dict) -> Task:
    entry = row.get("_bfcl_entry", row)
    if not isinstance(entry, dict):
        raise ValueError("BFCL entry must be a JSON object")
    if not isinstance(entry.get("question"), list) or not isinstance(entry.get("function"), list):
        raise ValueError("BFCL requires processed entries with question turns and function schemas")
    if not entry.get("involved_classes"):
        raise ValueError("BFCL online simulation requires executable stateful entries (involved_classes); AST-only rows have no tool backend")
    category = str(row.get("_bfcl_category") or row.get("category") or str(entry.get("id", "")).rsplit("_", 1)[0])
    if "memory" in category or entry.get("depends_on"):
        raise ValueError("BFCL memory/dependent entries require prerequisite scheduling; this runner supports independent stateful tasks only")
    if entry.get("missed_function"):
        raise ValueError("BFCL missed_function entries require per-turn tool changes and are not supported")
    if not entry["question"] or any(
        not isinstance(turn, list) or not turn or any(
            not isinstance(message, dict) or message.get("role") not in {"system", "user"}
            or not isinstance(message.get("content"), str) for message in turn
        ) for turn in entry["question"]
    ):
        raise ValueError("BFCL question must contain nonempty turns of system/user text messages")
    return Task("bfcl", _id(entry), category, "functions", "", payload={"entry": entry, "category": category})


def _terminal(row: dict) -> Task:
    raw = row.get("task_source_path") or row.get("task_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Terminal Bench task requires task_path or task_source_path")
    source = Path(raw).expanduser().resolve(strict=True)
    import yaml
    if not (source / "task.yaml").is_file():
        raise ValueError(f"Terminal Bench supports the task.yaml (v1) format, not Harbor task.toml: {source}")
    config = yaml.safe_load((source / "task.yaml").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("instruction"), str) or not config["instruction"].strip():
        raise ValueError(f"Terminal Bench task has no instruction: {source}")
    if not any((source / name).is_file() for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml", "Dockerfile")):
        raise ValueError(f"Terminal Bench task requires a Dockerfile or Compose file: {source}")
    # Native timing metadata is provenance only; simulations use the harness deadline.
    task_id = _id(row) if any(row.get(key) is not None for key in ("instance_id", "task_id", "id")) else source.name
    return Task("terminal-bench", task_id,
                str(config.get("category") or "dataset"), "terminal", config["instruction"],
                payload={"task_path": str(source), "config": config})


ADAPTERS: dict[str, Callable[[dict], Task]] = {
    "swe-rebench": lambda row: _repo_task("swe-rebench", row),
    "swe-bench-verified": lambda row: _repo_task("swe-bench-verified", row),
    "deep-research-bench": _research,
    "bfcl": _bfcl,
    "terminal-bench": _terminal,
}


def records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = next((value[key] for key in ("tasks", "instances", "data") if isinstance(value.get(key), list)), [value])
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("task source must contain JSON objects")
    return value


def load(name: str, source: Path) -> list[Task]:
    if name not in ADAPTERS:
        raise ValueError(f"unknown benchmark: {name}")
    source = source.expanduser().resolve(strict=True)
    if source.name == "task.yaml" and name == "terminal-bench":
        source = source.parent
    if source.is_dir() and name == "terminal-bench":
        if (source / "task.toml").exists():
            raise ValueError("Terminal Bench 2/Harbor task.toml format is not supported; supply v1 task.yaml tasks")
        paths = [source] if (source / "task.yaml").is_file() else sorted(p.parent for p in source.glob("*/task.yaml"))
        if not paths and any(source.glob("*/task.toml")):
            raise ValueError("Terminal Bench 2/Harbor task.toml format is not supported; supply v1 task.yaml tasks")
        rows = [{"task_path": str(p)} for p in paths]
    else:
        rows = records(source)
        if name == "terminal-bench":
            for row in rows:
                for key in ("task_path", "task_source_path"):
                    if row.get(key):
                        path = Path(row[key]).expanduser()
                        row[key] = str(path if path.is_absolute() else source.parent / path)
    tasks = [ADAPTERS[name](row) for row in rows]
    if len({task.key for task in tasks}) != len(tasks):
        raise ValueError("duplicate task IDs in benchmark source")
    if not tasks:
        raise ValueError("task source is empty")
    return tasks


def default_source(name: str, external: Path, root: Path) -> Path | None:
    """Resolve the default task roster, preferring files shipped by ClawTune.

    The external lookup is retained as a compatibility fallback for checkouts
    that intentionally remove the bundled smoke roster or want the historical
    sibling ``agent-test-bench`` layout without passing ``--dataset``.
    """
    bundled = root / BUNDLED_DATASETS[name] if name in BUNDLED_DATASETS else None
    if bundled is not None and (bundled.is_file() or bundled.is_dir()):
        return bundled

    directories = {
        "swe-rebench": ("swe-rebench",),
        "deep-research-bench": ("deep-research-bench",),
        "swe-bench-verified": ("swe-bench-verified", "swebench_verified"),
        "terminal-bench": ("terminal-bench",),
    }.get(name, ())
    candidates = [external / "data" / directory / "tasks.json" for directory in directories]
    if name == "terminal-bench":
        candidates += [external / "data/terminal-bench/tasks"]
    return next((path for path in candidates if path.is_file() or (
        name == "terminal-bench" and path.is_dir()
    )), None)


def default_config(name: str, root: Path = ROOT) -> Path:
    """Return the configuration used when ``benchmark`` gets no ``--config``.

    ``configs/benchmark.yaml`` is created by setup and is the user's local
    override.  The tracked benchmark-specific file is the clean-checkout
    fallback.  Legacy paths remain last-resort compatibility for older test
    trees and direct users of the former layout.
    """
    if name not in NAMES:
        raise ValueError(f"unknown benchmark: {name}")
    candidates = [root / "configs/benchmark.yaml", root / BUNDLED_CONFIGS[name]]
    legacy = {
        "swe-rebench": ("swe_rebench/config.yaml", "swe_rebench/config.example.yaml"),
        "deep-research-bench": (
            "deep_research_bench/config.yaml",
            "deep_research_bench/config.example.yaml",
        ),
    }
    candidates.extend(root / path for path in legacy.get(name, ()))
    candidates.append(root / "configs/benchmark.example.yaml")
    return next((path for path in candidates if path.is_file()), candidates[0])


def select(tasks: list[Task], *, sample: int | None, skip: int = 0, repo: str | None = None, ids: str | None = None) -> list[Task]:
    if skip < 0 or (sample is not None and sample <= 0):
        raise ValueError("sample must be positive and skip nonnegative")
    if repo:
        tasks = [task for task in tasks if task.repo == repo]
    if ids:
        by_id = {task.task_id: task for task in tasks}
        requested = ids.split(",")
        if len(set(requested)) != len(requested) or any(key not in by_id for key in requested):
            raise ValueError("instance IDs are duplicated or absent from selected source")
        tasks = [by_id[key] for key in requested]
    tasks = tasks[skip:]
    if sample is not None:
        if len(tasks) < sample:
            raise ValueError(f"requested {sample} tasks, only {len(tasks)} available")
        tasks = tasks[:sample]
    if not tasks:
        raise ValueError("no tasks selected")
    return tasks
