"""
Research prompt rendering for Deep Research Bench.

Adapts agent-test-bench's ``configs/prompts/deep_research_bench/default.md``:
replace ``{{task}}`` with the problem statement and append inference-time
metadata (topic / difficulty / domain) when present.
"""

from __future__ import annotations

import json
from pathlib import Path

from deep_research_bench.task_source import DRBTask

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

INFERENCE_METADATA_FIELDS = ("topic", "difficulty", "domain")


def load_drb_prompt_template(name: str = "default") -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template {name!r} not found at {path}")
    text = path.read_text(encoding="utf-8")
    if "{{task}}" not in text:
        raise ValueError(f"Prompt template {path} is missing '{{{{task}}}}'")
    return text


def drb_inference_metadata(task: DRBTask) -> dict[str, str]:
    return {
        key: value
        for key in INFERENCE_METADATA_FIELDS
        if (value := getattr(task, key))
    }


def render_drb_prompt(task: DRBTask, prompt_template: str = "default") -> str:
    template = load_drb_prompt_template(prompt_template)
    prompt = template.replace("{{task}}", str(task.problem_statement))
    metadata = drb_inference_metadata(task)
    if metadata:
        prompt += (
            "\n\nInference-time metadata:\n"
            + json.dumps(metadata, ensure_ascii=False, indent=2)
        )
    return prompt
