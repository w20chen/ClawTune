import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.adapters import (
    ADAPTERS,
    BUNDLED_CONFIGS,
    BUNDLED_DATASETS,
    NAMES,
    default_config,
    default_source,
    load,
)


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


def test_every_benchmark_has_a_tracked_default_config_and_roster(tmp_path):
    root = Path(__file__).resolve().parents[1]
    missing_external = tmp_path / "missing-agent-test-bench"
    for name in NAMES:
        config = default_config(name, root)
        source = default_source(name, missing_external, root)
        assert config == root / BUNDLED_CONFIGS[name]
        assert config.is_file()
        assert source == root / BUNDLED_DATASETS[name]
        assert source.is_file()


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


@pytest.mark.parametrize("task_platform", ["", "linux/arm64"])
def test_terminal_dockerfile_uses_native_build_context(monkeypatch, tmp_path, task_platform):
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
                "build": {"context": str(self.root)}, "volumes": [], "platform": task_platform}}}))
        return SimpleNamespace(stdout="container-id" if args[0] == "ps" else "")
    monkeypatch.setattr(TerminalBackend, "_run", invoke)
    monkeypatch.setattr("benchmarks.compose.compose_argv", lambda **kwargs: ["docker", "compose"])
    backend = TerminalBackend(load("terminal-bench", source)[0], output, platform="linux/amd64")
    override = backend.log_dir / "compose-platform.json"
    assert override.exists() == (not task_platform)
    if not task_platform:
        assert json.loads(override.read_text()) == {"services": {"client": {"platform": "linux/amd64"}}}
        assert backend.command[-2:] == ["-f", str(override)]
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
    monkeypatch.setattr(sys, "pycache_prefix", sys.pycache_prefix)
    monkeypatch.syspath_prepend(str(tmp_path))
    backends.ensure_bfcl()
    assert os.environ["BFCL_PROJECT_ROOT"] == str(tmp_path / "clawtune/.runtime/bfcl")
    module = package / "bfcl_eval" / "readonly_probe.py"
    module.write_text("value = 42\n")
    import importlib.util
    spec = importlib.util.spec_from_file_location("readonly_probe", module)
    # Capture the actual importer's write destination without depending on
    # Windows long-path support for an optional bytecode cache file.
    writes = []
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(spec.loader, "set_data", lambda path, data, **kw: writes.append(Path(path)))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    assert loaded.value == 42
    assert not (module.parent / "__pycache__").exists()
    assert Path(importlib.util.cache_from_source(str(module))).is_relative_to(
        tmp_path / "clawtune/.runtime/bfcl/pycache")
    assert writes == [Path(importlib.util.cache_from_source(str(module)))]


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


@pytest.mark.parametrize("key", ["instance_id", "task_id", "id"])
def test_terminal_preserves_manifest_identity(tmp_path, key):
    source = terminal_task(tmp_path / "task")
    manifest = tmp_path / "tasks.json"
    manifest.write_text(json.dumps([{key: "dataset-identity", "task_path": "task"}]))
    assert load("terminal-bench", manifest)[0].task_id == "dataset-identity"


def test_terminal_harbor_parent_has_actionable_error(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("version = '1.0'")
    with pytest.raises(ValueError, match="Harbor"):
        load("terminal-bench", tmp_path)


@pytest.mark.parametrize("timeout", [0, -1, True, ".inf", "invalid"])
def test_terminal_rejects_invalid_native_timeout(tmp_path, timeout):
    source = terminal_task(tmp_path / "task")
    with (source / "task.yaml").open("a") as handle:
        handle.write(f"max_agent_timeout_sec: {timeout}\n")
    with pytest.raises(ValueError, match="finite positive"):
        load("terminal-bench", source)


@pytest.mark.parametrize("outer,expected", [(None, 460), (200, 200), (900, 460)])
def test_terminal_native_budget_is_shared_across_calls(monkeypatch, outer, expected):
    from benchmarks import backends
    backend = backends.TerminalBackend.__new__(backends.TerminalBackend)
    backend.deadline, backend.agent_timeout = outer, 360
    monkeypatch.setattr(backends.time, "monotonic", lambda: 100)
    assert backend.start_agent() == expected
    monkeypatch.setattr(backends.time, "monotonic", lambda: expected - 10)
    assert backend._remaining(300) == 10
    monkeypatch.setattr(backends.time, "monotonic", lambda: expected + 1)
    with pytest.raises(TimeoutError):
        backend._remaining(300)
