from __future__ import annotations

import json

import pytest

from deep_research_bench.config import DRBConfig
from deep_research_bench.prompt import load_drb_prompt_template, render_drb_prompt
from deep_research_bench.task_source import (
    DRBTask,
    filter_tasks,
    load_tasks_from_drb_dataset,
    parse_instance_ids,
    task_to_swe_taskdef,
    tasks_from_records,
)


def _write_config(tmp_path, extra: str = "") -> DRBConfig:
    config = tmp_path / "config.yaml"
    config.write_text(
        "runtime:\n"
        '  mode: "host-openclaw"\n'
        + extra
        + "llm:\n"
        '  api_key: "test-key"\n',
        encoding="utf-8",
    )
    return DRBConfig.from_yaml(config, repo_root=tmp_path)


def test_drb_config_defaults_ebpf_off_and_gate_on(tmp_path) -> None:
    config = _write_config(tmp_path)
    assert config.runtime.mode == "host-openclaw"
    assert config.runtime.ebpf_required is False
    assert config.gate_required is True
    assert config.dataset.harness_dataset == "muset-ai/DeepResearch-Bench-Dataset"
    assert config.dataset.data_files == "generated_reports/openai-deepresearch.jsonl"
    assert config.dataset.prompt_template == "default"
    assert config.sandbox.image == "python:3.11-slim"


def test_drb_config_explicit_gate_required(tmp_path) -> None:
    config = _write_config(tmp_path, "  gate_required: false\n")
    assert config.gate_required is False


def test_drb_config_explicit_ebpf_required(tmp_path) -> None:
    config = _write_config(tmp_path, "  ebpf_required: true\n")
    assert config.runtime.ebpf_required is True


def test_drb_config_builds_swe_runner_config(tmp_path) -> None:
    config = _write_config(tmp_path)
    swe = config.to_swe_runner_config()
    assert swe.llm.api_key == "test-key"
    assert swe.runtime.mode == "host-openclaw"
    assert swe.repo_root == tmp_path
    assert swe.config_path is not None


def test_record_mapping_uses_drb_fields() -> None:
    records = [
        {
            "id": 7,
            "prompt": "research question?",
            "article": "long reference article",
            "topic": "physics",
            "difficulty": "phd",
            "domain": "science-technology",
        }
    ]
    task = tasks_from_records(records)[0]
    assert task.instance_id == "7"
    assert task.problem_statement == "research question?"
    assert task.reference_answer == "long reference article"
    assert task.topic == "physics"
    assert task.difficulty == "phd"
    assert task.domain == "science-technology"
    assert task.reference_kind == "generated_report"


def test_load_json_array_and_jsonl(tmp_path) -> None:
    json_path = tmp_path / "tasks.json"
    json_path.write_text(
        json.dumps([{"id": 1, "prompt": "a", "article": "b"}]),
        encoding="utf-8",
    )
    tasks = load_tasks_from_drb_dataset(json_path)
    assert [task.instance_id for task in tasks] == ["1"]

    jsonl_path = tmp_path / "tasks.jsonl"
    jsonl_path.write_text(
        '{"id": 2, "prompt": "c", "article": "d"}\n'
        '{"id": 3, "prompt": "e", "article": "f"}\n',
        encoding="utf-8",
    )
    tasks = load_tasks_from_drb_dataset(jsonl_path)
    assert [task.instance_id for task in tasks] == ["2", "3"]
    assert tasks[1].problem_statement == "e"


def test_load_wrapped_dict(tmp_path) -> None:
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"instances": [{"id": 9, "prompt": "q", "article": "a"}]}),
        encoding="utf-8",
    )
    tasks = load_tasks_from_drb_dataset(wrapped)
    assert tasks[0].instance_id == "9"


def test_filter_tasks_order_sample_skip_ids() -> None:
    tasks = [DRBTask(instance_id=f"t{i}", problem_statement=str(i)) for i in range(5)]
    assert [t.instance_id for t in filter_tasks(tasks, sample=2)] == ["t0", "t1"]
    assert [t.instance_id for t in filter_tasks(tasks, skip=2)] == ["t2", "t3", "t4"]
    assert [t.instance_id for t in filter_tasks(tasks, instance_ids=["t4", "t1"])] == [
        "t4",
        "t1",
    ]
    assert parse_instance_ids("a, b") == ["a", "b"]
    assert parse_instance_ids(None) is None


def test_task_to_swe_taskdef() -> None:
    task = DRBTask(instance_id="7", problem_statement="q")
    swe = task_to_swe_taskdef(task, "python:3.11-slim")
    assert swe.instance_id == "7"
    assert swe.problem_statement == "q"
    assert swe.image == "python:3.11-slim"


def test_prompt_render_replaces_task_and_appends_metadata() -> None:
    task = DRBTask(
        instance_id="1",
        problem_statement="Why is the sky blue?",
        topic="physics",
        difficulty="phd",
        domain="science-technology",
    )
    prompt = render_drb_prompt(task)
    assert "Why is the sky blue?" in prompt
    assert '"topic": "physics"' in prompt
    assert '"difficulty": "phd"' in prompt
    assert '"domain": "science-technology"' in prompt

    plain = DRBTask(instance_id="2", problem_statement="Q")
    assert "Inference-time metadata" not in render_drb_prompt(plain)


def test_prompt_template_missing_placeholder_raises(tmp_path, monkeypatch) -> None:
    import deep_research_bench.prompt as prompt_module

    monkeypatch.setattr(prompt_module, "_PROMPTS_DIR", tmp_path)
    (tmp_path / "bad.md").write_text("no placeholder here", encoding="utf-8")
    with pytest.raises(ValueError, match="task"):
        prompt_module.load_drb_prompt_template("bad")


def test_bundled_smoke_tasks_are_loadable() -> None:
    from pathlib import Path

    bundled = (
        Path(__file__).resolve().parents[1] / "deep_research_bench" / "tasks.json"
    )
    tasks = load_tasks_from_drb_dataset(bundled)
    assert len(tasks) >= 3
    assert all(task.problem_statement for task in tasks)


def test_web_search_config_defaults(tmp_path) -> None:
    config = _write_config(tmp_path)
    assert config.web_search.enabled is True
    assert config.web_search.provider == "tavily"
    assert config.web_search.api_key == ""
    assert config.web_search.api_key_file is not None
    assert config.web_search.api_key_file.name == "tavily_api_key.txt"


def test_web_search_config_key_from_file(tmp_path) -> None:
    key_file = tmp_path / "tavily_key.txt"
    key_file.write_text("tvly-file-key\n", encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "runtime:\n"
        '  mode: "host-openclaw"\n'
        "llm:\n"
        '  api_key: "k"\n'
        "web_search:\n"
        '  provider: "tavily"\n'
        f'  api_key_file: "{key_file.as_posix()}"\n',
        encoding="utf-8",
    )
    config = DRBConfig.from_yaml(config_file, repo_root=tmp_path)
    assert config.web_search.api_key == "tvly-file-key"


def test_web_search_config_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env-key")
    config = _write_config(tmp_path)
    assert config.web_search.api_key == "tvly-env-key"


def test_web_search_config_template_resolves_from_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env-key")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "runtime:\n"
        '  mode: "host-openclaw"\n'
        "llm:\n"
        '  api_key: "k"\n'
        "web_search:\n"
        '  api_key: "${TAVILY_API_KEY}"\n',
        encoding="utf-8",
    )
    config = DRBConfig.from_yaml(config_file, repo_root=tmp_path)
    assert config.web_search.api_key == "tvly-env-key"


def test_web_search_config_dotenv_fallback(tmp_path) -> None:
    (tmp_path / ".env").write_text(
        'TAVILY_API_KEY="tvly-dotenv-key"\n', encoding="utf-8"
    )
    config = _write_config(tmp_path)
    assert config.web_search.api_key == "tvly-dotenv-key"
