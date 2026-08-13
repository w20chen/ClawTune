"""
Task definition loading for Deep Research Bench.

Rows are research QA records: ``id`` / ``prompt`` / ``article`` (plus optional
``topic`` / ``difficulty`` / ``domain``), mirroring agent-test-bench's
DeepResearchBench task normalization.  Unlike SWE-Rebench there is no Docker
task image; the agent's tools run in a shared very basic sandbox container.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swe_rebench.task_source import TaskDef


@dataclass
class DRBTask:
    """A single Deep Research Bench task definition."""

    instance_id: str
    """Unique task identifier (the dataset ``id``)."""

    problem_statement: str
    """The research question / prompt for the agent to answer."""

    reference_answer: str = ""
    """Reference article (record-only in this MVP; used for offline grading)."""

    topic: str | None = None
    difficulty: str | None = None
    domain: str | None = None
    reference_kind: str = "generated_report"

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "problem_statement": self.problem_statement,
            "reference_answer": self.reference_answer,
            "topic": self.topic,
            "difficulty": self.difficulty,
            "domain": self.domain,
            "reference_kind": self.reference_kind,
        }


def load_tasks_from_drb_dataset(path: str | Path) -> list[DRBTask]:
    """Load tasks from a DeepResearchBench JSON/JSONL dataset file.

    Accepts a JSON array, a dict wrapping a list under ``instances``/``data``/
    ``tasks``, or one JSON object per line (JSONL).
    """
    raw_text = Path(path).read_text(encoding="utf-8")
    stripped = raw_text.strip()
    records = _parse_json_document(stripped)
    if records is None:
        records = _try_jsonl(raw_text)
    if not records:
        raise ValueError(f"No DeepResearchBench tasks found in {path}")
    return tasks_from_records(records)


def load_tasks_from_simple_list(path: str | Path) -> list[DRBTask]:
    """Load tasks from a simple JSON array file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array, got {type(data)}")
    return tasks_from_records(data)


def tasks_from_records(records: list[dict[str, Any]]) -> list[DRBTask]:
    """Convert raw task dictionaries to DRBTask objects."""
    return [_record_to_task(item) for item in records if isinstance(item, dict)]


def filter_tasks(
    tasks: list[DRBTask],
    *,
    sample: int | None = None,
    skip: int = 0,
    instance_ids: list[str] | None = None,
) -> list[DRBTask]:
    """Apply benchmark-style task selection.

    Explicit instance IDs preserve the user-provided order; then skip, then
    sample (matching swe_rebench.task_source).
    """
    selected = list(tasks)
    if instance_ids:
        by_id = {task.instance_id: task for task in selected}
        selected = [by_id[iid] for iid in instance_ids if iid in by_id]
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


def task_to_swe_taskdef(task: DRBTask, image: str) -> TaskDef:
    """Convert a DRB task to the swe-rebench TaskDef used by host_openclaw."""
    return TaskDef(
        instance_id=task.instance_id,
        image=image,
        problem_statement=task.problem_statement,
    )


def _parse_json_document(stripped: str) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        items = data.get("instances") or data.get("data") or data.get("tasks")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        if any(key in data for key in ("id", "instance_id", "task_id", "prompt")):
            return [data]
        raise ValueError(
            f"Cannot find tasks in JSON dict with keys: {list(data.keys())}"
        )
    return None


def _try_jsonl(text: str) -> list[dict[str, Any]] | None:
    """Parse one JSON object per line.  Returns None if any line is not JSON."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            return None
        if isinstance(rec, dict):
            records.append(rec)
        else:
            return None
    return records or None


def _record_to_task(record: dict[str, Any]) -> DRBTask:
    iid = (
        record.get("instance_id")
        or record.get("task_id")
        or record.get("id")
        or record.get("name")
        or "unknown"
    )
    problem = (
        record.get("problem_statement")
        or record.get("prompt")
        or record.get("question")
        or record.get("problem")
        or record.get("text")
        or ""
    )
    answer = (
        record.get("reference_answer")
        or record.get("article")
        or record.get("answer")
        or ""
    )
    return DRBTask(
        instance_id=str(iid),
        problem_statement=str(problem),
        reference_answer=str(answer),
        topic=_optional_text(record.get("topic")),
        difficulty=_optional_text(record.get("difficulty")),
        domain=_optional_text(record.get("domain")),
        reference_kind=str(record.get("reference_kind", "generated_report")),
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
