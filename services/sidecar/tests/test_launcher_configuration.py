from pathlib import Path

import pytest

from clawtune_sidecar import launcher


@pytest.mark.parametrize("mode", ["proc", "perf", "ksys", "vtune", "unknown"])
@pytest.mark.parametrize("backend", ["forkexec", "subprocess"])
def test_unsupported_profiler_rejected_before_payload_creation(monkeypatch, mode, backend):
    def forbidden(*args, **kwargs):
        pytest.fail("configuration rejection must precede payload creation")

    monkeypatch.setattr(launcher.os, "pipe", forbidden)
    monkeypatch.setattr(launcher.subprocess, "Popen", forbidden)
    runner = getattr(launcher, f"_run_{backend}_claimed")
    with pytest.raises(ValueError, match="unsupported profiling mode"):
        runner("http://unused", "unused", {"profiling": {"mode": mode}}, 1)


@pytest.mark.parametrize("enabled", [False, True])
def test_numa_switch_controls_explicit_memory_placement(monkeypatch, tmp_path, enabled):
    monkeypatch.setattr(launcher, "_supports_posix_controls", lambda: True)
    monkeypatch.setattr(launcher, "_cgroup_procs_writable", lambda _path: True)
    monkeypatch.setenv("CLAWTUNE_CGROUP_ROOT", str(tmp_path))
    monkeypatch.delenv("CLAWTUNE_CGROUP_PATH", raising=False)
    monkeypatch.delenv("CLAWTUNE_CGROUP_REQUIRED", raising=False)
    path = Path(launcher._prepare_cgroup(
        "numa-toggle", None, "1", {"enable_cgroup": True, "enable_numa": enabled},
    ))
    if enabled:
        assert (path / "cpuset.mems").read_text() == "1"
    else:
        assert not (path / "cpuset.mems").exists()
