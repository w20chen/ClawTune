import copy
import threading
import time

import httpx
import pytest

from command_parser import ParseContext, evidence
from command_parser.generation import GeneratorConfig, RuleGenerator
from command_parser.registry import RuleRegistry
from test_command_rule_generation import generated_rule


@pytest.fixture
def container_source(monkeypatch):
    content = {"text": "import argparse\np=argparse.ArgumentParser()\np.add_argument('-n')\np.parse_args()\n"}
    reads = []

    def read(entry, context):
        reads.append(threading.current_thread())
        return evidence.record("source", entry, content["text"], "synthetic-docker-archive")

    monkeypatch.setattr(evidence, "docker_source", read)
    context = ParseContext(repo="synthetic", runtime_id="synthetic", container_id="container",
                           workspace="/work", cwd="/work")
    return context, content, reads


def _drain(generator):
    deadline = time.monotonic() + 3
    while generator._queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.005)
    assert generator._queue.unfinished_tasks == 0, generator.status()


def test_container_edit_generates_new_identity_but_unchanged_source_is_deduplicated(
    tmp_path, container_source,
):
    context, content, reads = container_source
    calls = []
    registry = RuleRegistry(tmp_path)

    def client(body):
        calls.append(copy.deepcopy(body))
        return generated_rule()

    generator = RuleGenerator(registry, GeneratorConfig("unused", "synthetic", "synthetic"), client=client)
    try:
        assert generator.enqueue("task.py", context, script=True)
        _drain(generator)
        assert reads and all(thread is not threading.current_thread() for thread in reads)
        original, _ = registry.lookup("task.py", context, script=True)
        assert original is not None and len(calls) == 1
        assert generator.enqueue("task.py", context, script=True)  # only an asynchronous source check
        _drain(generator)
        assert len(calls) == 1 and generator._counts[context.repo] == 1
        content["text"] += "# edited source\n"
        assert registry.lookup("task.py", context, script=True)[0] is None
        assert generator.enqueue("task.py", context, script=True)
        _drain(generator)
        updated, _ = registry.lookup("task.py", context, script=True)
        assert updated["scope"]["source_hash"] != original["scope"]["source_hash"]
        assert len(calls) == 2 and generator._counts[context.repo] == 2
        assert len(generator.status()) == 2
        assert all(row["status"] == "ready" for row in generator.status().values())
        assert len(list(tmp_path.glob("*.json"))) == 2
    finally:
        generator.close()
    assert generator._queue.unfinished_tasks == 0


def test_container_failure_cooldown_applies_to_source_identity(container_source):
    context, content, _ = container_source
    calls = []

    def client(body):
        calls.append(body)
        if len(calls) == 1:
            raise httpx.ReadTimeout("synthetic timeout")
        return generated_rule()

    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("unused", "synthetic", "synthetic"), client=client)
    try:
        assert generator.enqueue("task.py", context, script=True)
        _drain(generator)
        assert generator.enqueue("task.py", context, script=True)
        _drain(generator)
        assert len(calls) == 1
        assert len(generator.status()) == 1
        assert next(iter(generator.status().values()))["status"] == "failed"
        content["text"] += "# new source after failed generation\n"
        assert generator.enqueue("task.py", context, script=True)
        _drain(generator)
        assert len(calls) == 2
        assert sorted(row["status"] for row in generator.status().values()) == ["failed", "ready"]
    finally:
        generator.close()


def test_container_source_checks_do_not_spend_generation_budget(container_source):
    context, content, _ = container_source
    calls = []
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("unused", "synthetic", "synthetic", repo_budget=2),
                              client=lambda body: calls.append(body) or generated_rule())
    try:
        for _ in range(3):
            assert generator.enqueue("task.py", context, script=True)
            _drain(generator)
        assert len(calls) == 1
        content["text"] += "# change\n"
        assert generator.enqueue("task.py", context, script=True)
        _drain(generator)
        assert len(calls) == 2
        content["text"] += "# budget exhausted\n"
        assert not generator.enqueue("task.py", context, script=True)
    finally:
        generator.close()


def test_container_source_read_is_deferred_until_idle_and_close_acknowledges_queue(container_source):
    context, _, reads = container_source

    class Busy:
        idle = staticmethod(lambda: False)

    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("unused", "synthetic", "synthetic"),
                              help_runner=Busy(), client=lambda body: pytest.fail("provider ran while busy"))
    assert generator.enqueue("task.py", context, script=True)
    assert not generator.enqueue("task.py", context, script=True)
    assert generator.enqueue("other.py", context, script=True)
    generator.close(timeout_seconds=2)
    assert not reads
    assert generator._queue.empty() and generator._queue.unfinished_tasks == 0
    assert not generator._thread.is_alive()
    assert all(row["reason"] == "rule_generation_cancelled" for row in generator.status().values())
    assert not generator.enqueue("after.py", context, script=True)


def test_close_during_container_source_read_prevents_provider_and_cache_activation(monkeypatch, container_source):
    context, _, _ = container_source
    entered, release = threading.Event(), threading.Event()

    def blocked_read(entry, context):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(3)
        return evidence.record("source", entry, "# -n COUNT", "synthetic-docker-archive")

    monkeypatch.setattr(evidence, "docker_source", blocked_read)
    generator = RuleGenerator(RuleRegistry(), GeneratorConfig("unused", "synthetic", "synthetic"),
                              client=lambda body: pytest.fail("provider ran after cancellation"))
    try:
        assert generator.enqueue("task.py", context, script=True)
        assert entered.wait(1)
        assert not generator.enqueue("task.py", context, script=True)
        with pytest.raises(TimeoutError):
            generator.close(timeout_seconds=.01)
    finally:
        release.set()
        generator.close(timeout_seconds=2)
    assert generator._queue.unfinished_tasks == 0
    assert all(row["status"] == "failed" for row in generator.status().values())
