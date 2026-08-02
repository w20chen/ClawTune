"""
Task definition loading for swe-rebench.

Supports multiple formats:
- Native swe-rebench JSON/JSONL dataset files
- Simple JSON list
- Manual inline task specification
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskDef:
    """A single swe-rebench task definition."""

    instance_id: str
    """Unique task identifier (e.g. ``django__django-12345``)."""

    image: str
    """Docker image name (e.g. ``swerebench/sweb.eval.x86_64.django:latest``)."""

    problem_statement: str = ""
    """The GitHub issue / problem description for the agent to solve."""

    repo: str = ""
    """Repository slug (e.g. ``django/django``)."""

    base_commit: str = ""
    """Base commit hash the image is checked out at."""

    hint_text: str = ""
    """Optional hint / additional instructions."""

    extra_env: dict[str, str] = field(default_factory=dict)
    """Additional environment variables to pass to the container."""


def infer_repo_from_instance_id(instance_id: str) -> str | None:
    """Infer a SWE-bench repository slug from ``owner__repo-issue``.

    Dataset-provided repository metadata remains authoritative.  This fallback
    keeps older/simple task lists correctly namespaced without collapsing
    unrelated repositories into one scheduler default.
    """

    owner, separator, repo_and_issue = instance_id.strip().partition("__")
    if separator != "__" or not owner or not repo_and_issue:
        return None
    repo_name, issue_separator, issue = repo_and_issue.rpartition("-")
    if issue_separator != "-" or not repo_name or not issue.isdigit():
        return None
    return f"{owner}/{repo_name}"


def task_repo_key(task: TaskDef) -> str:
    """Return the stable KB namespace for a task's target repository."""

    explicit = task.repo.strip()
    if explicit:
        return explicit
    inferred = infer_repo_from_instance_id(task.instance_id)
    if inferred is not None:
        return inferred
    return f"instance:{task.instance_id}"


def load_tasks_from_swebench_dataset(path: str | Path) -> list[TaskDef]:
    """Load tasks from a standard SWE-bench / SWE-rebench dataset file.

    The dataset is typically a JSON or JSONL file.  JSON files may contain
    a top-level array or a dict with an ``instances`` or ``data`` key.
    """
    path = Path(path)
    raw_text = path.read_text(encoding="utf-8")

    stripped = raw_text.strip()

    # Try single JSON parse first (handles array-of-objects and single-object)
    try:
        data = json.loads(stripped)
        if isinstance(data, list):
            return [_record_to_task(r) for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            # Might be a task record itself, or a wrapper with "instances"/"data"/"tasks"
            items = data.get("instances") or data.get("data") or data.get("tasks")
            if isinstance(items, list):
                return [_record_to_task(r) for r in items if isinstance(r, dict)]
            # Single task record
            if data.get("instance_id") or data.get("task_id") or data.get("docker_image"):
                return [_record_to_task(data)]
            raise ValueError(f"Cannot find tasks in JSON dict with keys: {list(data.keys())}")
    except (json.JSONDecodeError, ValueError):
        pass

    # JSONL: one JSON object per line
    if stripped.startswith("{"):
        records = _try_jsonl(raw_text)
        if records is not None:
            return [_record_to_task(r) for r in records]

    # Plain list fallback (one JSON value per line, may be objects or arrays)
    tasks: list[TaskDef] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        tasks.append(_record_to_task(item))
            elif isinstance(parsed, dict):
                tasks.append(_record_to_task(parsed))
        except json.JSONDecodeError:
            continue
    return tasks


def load_tasks_from_simple_list(path: str | Path) -> list[TaskDef]:
    """Load tasks from a simple JSON file.

    Expected format::

        [
          {
            "instance_id": "task-1",
            "image": "swerebench/...:latest",
            "problem_statement": "..."
          }
        ]
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array, got {type(data)}")
    return tasks_from_records(data)


def tasks_from_records(records: list[dict[str, Any]]) -> list[TaskDef]:
    """Convert raw task dictionaries to TaskDef objects."""
    return [_record_to_task(item) for item in records if isinstance(item, dict)]


def filter_tasks(
    tasks: list[TaskDef],
    *,
    sample: int | None = None,
    skip: int = 0,
    instance_ids: list[str] | None = None,
    repo: str | None = None,
) -> list[TaskDef]:
    """Apply benchmark-style task selection.

    Selection order follows agent-test-bench's user-facing flow:
    first narrow by explicit instance IDs or repo, then apply skip/sample.
    Explicit instance IDs preserve the user-provided order.
    """
    selected = list(tasks)

    if instance_ids:
        by_id = {task.instance_id: task for task in selected}
        selected = [by_id[iid] for iid in instance_ids if iid in by_id]
    elif repo:
        selected = [task for task in selected if task.repo == repo]

    if skip > 0:
        selected = selected[skip:]

    if sample is not None and sample > 0:
        selected = selected[:sample]

    return selected


def parse_instance_ids(value: str | None) -> list[str] | None:
    """Parse a comma-separated instance ID list."""
    if value is None:
        return None
    ids = [item.strip() for item in value.split(",") if item.strip()]
    return ids or None


def create_single_task(
    instance_id: str,
    image: str,
    problem_statement: str = "",
    **kwargs: Any,
) -> TaskDef:
    """Create a single task definition inline."""
    if not str(kwargs.get("repo") or "").strip():
        inferred = infer_repo_from_instance_id(instance_id)
        if inferred is not None:
            kwargs["repo"] = inferred
    return TaskDef(
        instance_id=instance_id,
        image=image,
        problem_statement=problem_statement,
        **kwargs,
    )


def _try_jsonl(text: str) -> list[dict[str, Any]] | None:
    """Try parsing as JSONL (one JSON object per line).  Returns None if not all lines are JSON."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                records.append(rec)
            else:
                return None
        except json.JSONDecodeError:
            return None
    return records if records else None


def _record_to_task(record: dict[str, Any]) -> TaskDef:
    """Map a raw swe-bench record dict to a ``TaskDef``."""

    # instance_id ── many possible key names
    iid = (
        record.get("instance_id")
        or record.get("task_id")
        or record.get("id")
        or record.get("name")
        or "unknown"
    )

    # image
    image = (
        record.get("docker_image")
        or record.get("image")
        or record.get("container_image")
        or record.get("image_name")
        or ""
    )
    # swe-rebench sometimes has "image_name" + "image_tag"
    if not image:
        name = record.get("image_name", "")
        tag = record.get("image_tag", "latest")
        if name:
            image = f"{name}:{tag}"

    # problem statement
    problem = (
        record.get("problem_statement")
        or record.get("issue_text")
        or record.get("problem")
        or record.get("description")
        or record.get("text")
        or ""
    )
    # swe-bench often has problem_statement under a nested key
    if not problem and isinstance(record.get("task"), dict):
        problem = record["task"].get("problem_statement", "")
    if not problem and isinstance(record.get("issue"), dict):
        problem = record["issue"].get("body", "")

    # repo
    repo = str(record.get("repo") or record.get("repository") or "").strip()
    if not repo:
        repo = infer_repo_from_instance_id(str(iid)) or ""

    # base commit
    base = (
        record.get("base_commit")
        or record.get("base_sha")
        or record.get("commit")
        or ""
    )

    # hint
    hint = record.get("hint_text") or record.get("hints_text") or record.get("hint") or ""

    # extra env
    extra: dict[str, str] = {}
    for key, val in record.items():
        if key.startswith("env_") and isinstance(val, str):
            extra[key[4:]] = val
    if isinstance(record.get("environment"), dict):
        for k, v in record["environment"].items():
            extra[str(k)] = str(v)

    return TaskDef(
        instance_id=str(iid),
        image=str(image),
        problem_statement=str(problem),
        repo=str(repo),
        base_commit=str(base),
        hint_text=str(hint),
        extra_env=extra,
    )
