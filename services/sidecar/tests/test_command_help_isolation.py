"""Real Docker probe acceptance is opt-in; unit coverage checks fail-closed setup."""
import io
import json
import os
import subprocess

import pytest

from command_parser import ParseContext
from command_parser.help_runner import HelpRunner


def test_help_probe_pins_image_clears_secrets_and_cleans_up(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")

    def run(argv, **kwargs):
        commands.append(argv)
        assert "PROVIDER_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, json.dumps([{
            "Id": "sha256:" + "a" * 64,
            "Config": {"Env": ["PATH=/bin", "PROVIDER_API_KEY=secret"]},
        }]).encode())

    class Process:
        returncode = 0
        stdout = io.BytesIO(b"usage: tool [-n COUNT]")

        def __init__(self, argv, **kwargs):
            commands.append(argv)

        def poll(self):
            return 0

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Process)
    context = ParseContext(image="test:latest", workspace="/work", host_workspace=str(tmp_path))
    assert HelpRunner().acquire(["tool", "--help"], context)
    launch = commands[1]
    assert {"--network=none", "--read-only", "--cap-drop=ALL", "--pids-limit=64",
            "--memory=256m", "--cpus=1", "PROVIDER_API_KEY="} <= set(launch)
    assert "test:latest" not in launch and "sha256:" + "a" * 64 in launch
    assert commands[-1][:3] == ["docker", "rm", "-f"]


@pytest.mark.skipif(not os.getenv("CLAWTUNE_TEST_HELP_IMAGE"), reason="requires an isolated Docker image with python3")
def test_real_help_probe_cannot_write_workspace_or_use_network(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text("""import os, socket
assert not os.environ.get('CLAWTUNE_PROBE_SECRET')
try:
    open('/work/forbidden', 'w').write('x')
    raise AssertionError('writable workspace')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 80), timeout=.3)
    raise AssertionError('network available')
except OSError:
    pass
print('isolated-help-ok')
""")
    context = ParseContext(image=os.environ["CLAWTUNE_TEST_HELP_IMAGE"], workspace="/work",
                           host_workspace=str(tmp_path), cwd="/work")
    result = HelpRunner().acquire(["python3", "/work/probe.py", "--help"], context)
    assert result and "isolated-help-ok" in result["text"]
    assert not (tmp_path / "forbidden").exists()
