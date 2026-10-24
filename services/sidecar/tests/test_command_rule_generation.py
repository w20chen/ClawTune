import copy
import time

import pytest
from command_parser import CommandFeatureService, ParseContext
from command_parser.evidence import local_source
from command_parser.generation import GeneratorConfig, RuleGenerator
from command_parser.help_runner import HelpRunner, help_argv
from command_parser.registry import RuleRegistry
from clawtune_sidecar.config import SidecarConfig


def generated_rule(entry="task.py"):
    registry = RuleRegistry()
    rule, _ = registry.lookup("head", ParseContext())
    rule["grammar"]["options"] = [o for o in rule["grammar"]["options"] if "-n" in o["names"]]
    rule["grammar"]["options"][0]["names"] = ["-n"]
    rule["grammar"]["options"][0]["canonical"] = "-n"
    rule["fixtures"] = [{"argv": [entry, "-n", "4"], "status": "complete", "target": []}]
    return rule


def test_generator_uses_same_api_key_as_llm_proxy(monkeypatch):
    monkeypatch.setenv("CLAWTUNE_COMMAND_RULE_LLM_API_KEY", "obsolete-separate-key")
    config = SidecarConfig(
        llm_proxy_upstream_base_url="https://provider.example",
        llm_proxy_upstream_api_key="shared-proxy-key",
        llm_proxy_upstream_model="shared-model",
    )

    generated = GeneratorConfig.from_sidecar(config)

    assert generated.api_key == config.llm_proxy_upstream_api_key
    assert generated.base_url == config.llm_proxy_upstream_base_url
    assert generated.model == config.llm_proxy_upstream_model
    assert GeneratorConfig.from_sidecar(SidecarConfig(
        llm_proxy_upstream_base_url="https://provider.example",
        llm_proxy_upstream_model="shared-model",
    )).api_key is None


def test_repository_rule_uses_same_parser_and_cache(tmp_path):
    script = tmp_path / "task.py"
    script.write_text("import argparse\np = argparse.ArgumentParser()\np.add_argument('-n')\np.parse_args()\n")
    context = ParseContext(repo="repo", runtime_id="r", workspace=str(tmp_path), cwd=str(tmp_path))
    registry = RuleRegistry(tmp_path / "cache")
    generator = RuleGenerator(registry, GeneratorConfig("https://example.invalid", "test", "test"), client=lambda body: generated_rule())
    service = CommandFeatureService(registry, generator)
    before = service.argv(["python", "task.py", "-n", "4"], context)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(v["status"] == "ready" for v in generator.status().values()):
            time.sleep(.01)
        assert any(v["status"] == "ready" for v in generator.status().values()), generator.status()
        after = service.argv(["python", "task.py", "-n", "4"], context)
        assert before.status == "partial" and after.status == "complete"
        assert before.core == after.core
        assert any(f.startswith("opt:") for f in after.features)
        assert list((tmp_path / "cache").glob("*.json"))
        other = ParseContext(repo="other", runtime_id="r", workspace=str(tmp_path), cwd=str(tmp_path))
        assert registry.lookup("task.py", other, script=True)[0] is None
        script.write_text("# changed source\n")
        assert registry.lookup("task.py", context, script=True)[0] is None
    finally:
        service.close()


@pytest.mark.parametrize("entry,canonical", [("unknown-tool", "unknown-tool"), ("python3", "python")])
def test_unknown_tool_help_generates_same_rule(tmp_path, entry, canonical):
    class Help:
        def acquire(self, argv, context):
            assert argv == [entry, "--help"]
            from command_parser.evidence import record
            return record("help", argv[0], "usage: unknown-tool [-n COUNT] [FILE...]", "fixture")
    registry = RuleRegistry(tmp_path / "cache")
    generator = RuleGenerator(registry, GeneratorConfig("https://example.invalid", "test", "test"),
                              client=lambda body: generated_rule(entry), help_runner=Help())
    context = ParseContext(repo="r", runtime_id="runtime")
    rule = generator.generate(entry, context)
    assert rule["canonical"] == canonical
    registry.add(rule)
    assert CommandFeatureService(registry).argv([entry, "-n", "4"], context).status == "complete"


def test_no_evidence_does_not_call_model():
    calls = []
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("https://example.invalid", "test", "test"), client=lambda body: calls.append(body))
    with pytest.raises(ValueError, match="no_source"):
        generator.generate("unknown", ParseContext())
    assert calls == []


def test_llm_unknown_option_is_rejected(tmp_path):
    (tmp_path / "task.py").write_text("# no options here")
    context = ParseContext(repo="r", runtime_id="r", workspace=str(tmp_path), cwd=str(tmp_path))
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("https://example.invalid", "test", "test"), client=lambda body: copy.deepcopy(generated_rule()))
    with pytest.raises(ValueError, match="absent from evidence"):
        generator.generate("task.py", context, True)


def test_evidence_cannot_escape_registered_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "private.py").write_text("secret")
    context = ParseContext(workspace=str(root), cwd=str(root))
    assert local_source("../private.py", context) is None


def test_help_without_isolation_never_executes(monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("unrestricted help execution"))
    assert HelpRunner().acquire(["unknown", "--help"], ParseContext()) is None
    assert HelpRunner(idle=lambda: False).acquire(["unknown", "--help"], ParseContext(image="tool")) is None
    assert help_argv("script.py", script=True) == []


def test_discovery_reads_declared_entries_without_executing_manifest(tmp_path):
    from command_parser.evidence import discover
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.py").write_text("raise RuntimeError('must not execute')")
    (tmp_path / "pyproject.toml").write_text('[project.scripts]\nmytool = "cli:main"\n')
    context = ParseContext(workspace=str(tmp_path), cwd=str(tmp_path))
    assert "src/cli.py" in [p.replace("\\", "/") for p in discover(context)]


def test_generation_is_cancelled_while_measurement_is_active(tmp_path):
    class BusyHelp:
        idle = staticmethod(lambda: False)
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("https://example.invalid", "test", "test"),
                              client=lambda body: pytest.fail("generation overlapped measurement"), help_runner=BusyHelp())
    assert generator.enqueue("unknown", ParseContext())
    generator.close()
    assert not generator._thread.is_alive()


def test_generated_rule_requires_consistent_fixtures(tmp_path):
    (tmp_path / "task.py").write_text("# -n COUNT")
    rule = generated_rule()
    rule["fixtures"][0]["status"] = "invalid"
    context = ParseContext(repo="r", workspace=str(tmp_path), cwd=str(tmp_path))
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("https://example.invalid", "test", "test"), client=lambda body: copy.deepcopy(rule))
    with pytest.raises(ValueError, match="fixture mismatch"):
        generator.generate("task.py", context, True)


def test_local_parser_dependency_changes_source_identity(tmp_path):
    from command_parser.evidence import source
    (tmp_path / "task.py").write_text("from options import parser\n")
    helper = tmp_path / "options.py"
    helper.write_text("# --count COUNT\n")
    context = ParseContext(workspace=str(tmp_path), cwd=str(tmp_path))
    before = source("task.py", context)
    assert "--count COUNT" in before["text"]
    helper.write_text("# --count COUNT --verbose\n")
    assert source("task.py", context)["content_hash"] != before["content_hash"]


def test_generated_module_fixture_retains_interpreter_rule():
    class Help:
        def acquire(self, argv, context):
            from command_parser.evidence import record
            return record("help", "study_module", "usage: study_module [-n COUNT]", "fixture")

    rule = generated_rule("study_module")
    rule["fixtures"] = [{"argv": ["python3", "-m", "study_module", "-n", "4"],
                         "status": "complete", "target": ["study_module"]}]
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("https://example.invalid", "test", "test"),
                              client=lambda body: copy.deepcopy(rule), help_runner=Help())
    generated = generator.generate("study_module", ParseContext(repo="r", runtime_id="r"),
                                   argv=["python3", "-m", "study_module", "-n", "4"])
    assert generated["fixtures"] == rule["fixtures"]


def test_fixture_retry_reports_actual_parser_identity(tmp_path):
    (tmp_path / "task.py").write_text("# -n COUNT")
    context = ParseContext(repo="r", workspace=str(tmp_path), cwd=str(tmp_path))
    seen = []

    def response(body):
        seen.append(copy.deepcopy(body))
        rule = generated_rule()
        if len(seen) == 1:
            rule["fixtures"][0]["target"] = ["task.py", "4"]
        return rule

    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("https://example.invalid", "test", "test"), client=response)
    generator.generate("task.py", context, True)
    assert len(seen) == 2
    repair = seen[1]["messages"][-1]["content"]
    assert '"actual":{"status":"complete","target":[]}' in repair
    assert '"argv":["task.py","-n","4"]' in repair


def test_generated_script_fixture_accepts_full_interpreter_invocation(tmp_path):
    (tmp_path / "task.py").write_text("# -n COUNT")
    context = ParseContext(repo="r", workspace=str(tmp_path), cwd=str(tmp_path))
    rule = generated_rule()
    rule["fixtures"] = [{"argv": ["python", "task.py", "-n", "4"],
                         "status": "complete", "target": ["task.py"]}]
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("https://example.invalid", "test", "test"),
                              client=lambda body: copy.deepcopy(rule))
    assert generator.generate("task.py", context, True)["fixtures"] == rule["fixtures"]
