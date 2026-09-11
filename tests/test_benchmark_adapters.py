import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.adapters import ADAPTERS, default_source, load


def terminal_task(path):
    path.mkdir(parents=True)
    (path / "task.yaml").write_text("instruction: inspect this environment\ncategory: system\n")
    (path / "Dockerfile").write_text("FROM alpine\n")
    return path


@pytest.mark.parametrize("name,directory", [
    ("swe-rebench", "swe-rebench"),
    ("deep-research-bench", "deep-research-bench"),
    ("swe-bench-verified", "swebench_verified"),
])
def test_default_source_uses_actual_dataset_directory(tmp_path, name, directory):
    source = tmp_path / "external/data" / directory / "tasks.json"
    source.parent.mkdir(parents=True)
    source.write_text("[]")
    assert default_source(name, tmp_path / "external", tmp_path / "root") == source


def test_terminal_manifest_paths_are_relative_to_manifest(monkeypatch, tmp_path):
    source = terminal_task(tmp_path / "data/tasks/example")
    manifest = tmp_path / "data/list.jsonl"
    manifest.write_text(json.dumps({"task_source_path": "tasks/example"}))
    monkeypatch.chdir(tmp_path)
    assert load("terminal-bench", manifest)[0].payload["task_path"] == str(source)
    assert load("terminal-bench", source / "task.yaml")[0].task_id == "example"


def test_terminal_rejects_missing_path_and_harbor_format(tmp_path):
    with pytest.raises(ValueError, match="requires task_path"):
        ADAPTERS["terminal-bench"]({})
    (tmp_path / "task.toml").write_text("version = '1.0'")
    with pytest.raises(ValueError, match="Harbor"):
        load("terminal-bench", tmp_path)


def test_terminal_dockerfile_uses_native_build_context(monkeypatch, tmp_path):
    from benchmarks.backends import TerminalBackend
    source = terminal_task(tmp_path / "input/task")
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    output = tmp_path / "run"
    output.mkdir()
    calls = []
    def invoke(self, args, **kwargs):
        calls.append(args)
        assert self.env["T_BENCH_TASK_DOCKER_NAME_PREFIX"] == self.project
        if args[0] == "config":
            return SimpleNamespace(stdout=json.dumps({"services": {"client": {
                "build": {"context": str(self.root)}, "volumes": []}}}))
        return SimpleNamespace(stdout="container-id" if args[0] == "ps" else "")
    monkeypatch.setattr(TerminalBackend, "_run", invoke)
    backend = TerminalBackend(load("terminal-bench", source)[0], output)
    assert (backend.root / "docker-compose.yaml").exists()
    assert backend.container == "container-id"
    backend.close()
    assert calls[-1] == ["down", "--volumes", "--remove-orphans"]
    assert {p.name: p.read_bytes() for p in source.iterdir()} == original


@pytest.mark.parametrize("extra", [
    {"id": "memory_kv_1"}, {"depends_on": ["task-0"]},
    {"missed_function": {"1": [{"name": "later"}]}},
])
def test_bfcl_rejects_unimplemented_conversation_semantics(extra):
    entry = {"id": "multi_turn_base_1", "question": [[{"role": "user", "content": "work"}]],
             "function": [], "involved_classes": ["Counter"], **extra}
    with pytest.raises(ValueError, match="prerequisite|per-turn"):
        ADAPTERS["bfcl"](entry)


@pytest.mark.parametrize("package_path", [False, True])
def test_bfcl_bootstrap_accepts_repo_or_package_and_isolates_writes(monkeypatch, tmp_path, package_path):
    from benchmarks import backends
    package = tmp_path / "gorilla/berkeley-function-call-leaderboard"
    (package / "bfcl_eval").mkdir(parents=True)
    monkeypatch.setenv("BFCL_REPO_PATH", str(package if package_path else package.parent))
    monkeypatch.setenv("BFCL_PROJECT_ROOT", str(package))
    monkeypatch.setattr(backends, "ROOT", tmp_path / "clawtune")
    monkeypatch.syspath_prepend(str(tmp_path))
    backends.ensure_bfcl()
    assert os.environ["BFCL_PROJECT_ROOT"] == str(tmp_path / "clawtune/.runtime/bfcl")


def test_research_key_is_task_local(monkeypatch, tmp_path):
    from deep_research_bench.host_runner import _apply_web_search_key
    from swe_rebench.host_openclaw import _openclaw_env
    monkeypatch.setenv("TAVILY_API_KEY", "parent")
    for index in range(2):
        config = SimpleNamespace(docker=SimpleNamespace(cgroup_required=False, platform=""))
        _apply_web_search_key(SimpleNamespace(web_search=SimpleNamespace(enabled=True, api_key=str(index))), config)
        env = _openclaw_env(tmp_path / str(index), 8765, config, tmp_path / "workspace")
        assert env["TAVILY_API_KEY"] == str(index)
    assert os.environ["TAVILY_API_KEY"] == "parent"


def test_research_workdir_cannot_silently_disagree_with_mount():
    from deep_research_bench.config import SandboxConfig
    with pytest.raises(ValueError, match="/workspace"):
        SandboxConfig.from_dict({"workdir": "/another"})


def test_offline_verified_historical_identity():
    from offline.runner import canonical_benchmark
    assert canonical_benchmark("swebench_verified") == "swe-bench-verified"


def test_exported_llm_key_works_without_yaml_placeholder(monkeypatch, tmp_path):
    from swe_rebench.config import LLMConfig
    monkeypatch.setenv("LLM_API_KEY", "exported-key")
    config = LLMConfig.from_dict({"model": "test"}, tmp_path)
    assert config.api_key == "exported-key"
