"""Selection isolation and command compatibility for the optional cache tool."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("benchmark_cache", Path(__file__).resolve().parents[1] / "scripts/benchmark_cache.py")
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


def test_separate_counts_cap_and_input_preservation(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    sources = []
    for name in ("swe-rebench", "swe-bench-verified"):
        path = inputs / (name + ".json")
        path.write_text(json.dumps([{"instance_id": f"owner__repo-{i}", "problem_statement": "fix",
                                     "docker_image": f"example/{name}:{i}"} for i in range(4)]))
        sources.append(path)
    before = [p.read_bytes() for p in sources]
    args = argparse.Namespace(directory=tmp_path / "cache", dataset=[f"{p.stem}={p}" for p in sources],
                              config=[], swe_rebench=2, swe_bench_verified=30, deep_research_bench=0,
                              terminal_bench=0, bfcl=5, download_missing=False, platform="linux/amd64", sudo=False)
    cache.prepare(args)
    manifest = json.loads((args.directory / "manifest.json").read_text())
    assert manifest["selections"]["swe-rebench"]["ids"] == ["owner__repo-0", "owner__repo-1"]
    assert manifest["selections"]["swe-bench-verified"]["selected"] == 4
    assert len(manifest["images"]) == 6
    assert manifest["selections"]["bfcl"]["status"] == "no_task_images_required"
    assert [p.read_bytes() for p in sources] == before


def test_source_tree_is_never_output(tmp_path):
    source = tmp_path / "tasks.json"
    source.write_text("[]")
    args = argparse.Namespace(directory=tmp_path / "cache", dataset=[f"swe-rebench={source}"], config=[])
    with pytest.raises(ValueError, match="outside"):
        cache.prepare(args)
    assert not args.directory.exists()


def test_dockerfile_base_resolution_handles_stages_args_and_unrelated_shell():
    text = 'ARG BASE=ubuntu:22.04\nFROM ${BASE} AS builder\nRUN echo "unterminated\nFROM builder AS final\n'
    assert cache.base_images(text, {}) == {"ubuntu:22.04"}
    assert cache.base_images(text, {"BASE": "debian:13.0-slim"}) == {"debian:13.0-slim"}
    with pytest.raises(ValueError, match="unresolved"):
        cache.base_images("FROM ${UNKNOWN}", {})


def test_original_build_argv_and_registry_probe():
    job = {"image": "ctpre-task-image", "dockerfile": "/tmp/task/Dockerfile", "context": "/tmp/task",
           "args": {"A": "a b"}, "target": "final"}
    assert cache.build_command(job, {"docker": ["docker"], "platform": "linux/amd64"}) == [
        "docker", "build", "--platform", "linux/amd64", "--pull=false", "-t", "ctpre-task-image",
        "-f", "/tmp/task/Dockerfile", "--build-arg", "A=a b", "--target", "final", "/tmp/task"]
    assert cache.registry_url("swerebench/task:latest") == "https://registry-1.docker.io/v2/"
    assert cache.registry_url("ghcr.io/laude-institute/t-bench/image") == "https://ghcr.io/v2/"


def test_pull_retries_network_failure_and_uses_original_command(tmp_path, monkeypatch):
    inspections = iter([None, {"id": "sha256:example", "digests": []}])
    monkeypatch.setattr(cache, "available", lambda *a: next(inspections))
    probes = []
    def probe(image):
        probes.append(image)
        if len(probes) == 1:
            raise RuntimeError("tunnel temporarily unavailable")
    monkeypatch.setattr(cache, "network_probe", probe)
    sleeps, commands = [], []
    monkeypatch.setattr(cache.time, "sleep", sleeps.append)
    monkeypatch.setattr(cache.subprocess, "run", lambda argv, **kw: commands.append((argv, kw)))
    result = cache.pull("example/task:tag", 1,
                        {"docker": ["sudo", "-n", "docker"], "platform": "linux/amd64"}, tmp_path)
    assert result["status"] == "pulled" and result["attempts"] == 2
    assert sleeps == [30]
    assert commands[0][0] == ["sudo", "-n", "docker", "pull", "--platform", "linux/amd64", "example/task:tag"]
    assert commands[0][1]["timeout"] == 1800
