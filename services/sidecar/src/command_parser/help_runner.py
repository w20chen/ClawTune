from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid

from .evidence import LIMIT, record
from .models import ParseContext


def help_argv(entry: str, *, script: bool = False, interpreter: str | None = None) -> list[str]:
    if script:
        if not interpreter:
            return []
        return [interpreter, entry, "--help"]
    return [entry, "--help"]


class HelpRunner:
    """Fail closed when no isolated image/mount identity is available."""
    def __init__(self, idle=lambda: True, gate=None) -> None:
        self.idle = idle
        self.gate = gate or threading.RLock()

    def acquire(self, argv: list[str], context: ParseContext) -> dict | None:
        with self.gate:
            return self._acquire(argv, context)

    def _acquire(self, argv: list[str], context: ParseContext) -> dict | None:
        if (not argv or not self.idle() or not context.image or not shutil.which("docker")
                or (context.workspace and not context.host_workspace)):
            return None
        if argv[0].startswith("-") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", context.image):
            return None
        if any("," in (p or "") for p in (context.workspace, context.host_workspace)):
            return None
        environment = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "HOME"}}
        try:
            inspected = subprocess.run(["docker", "image", "inspect", context.image], capture_output=True,
                                       timeout=3, check=True, env=environment)
            info = json.loads(inspected.stdout)[0]
            image = info["Id"]  # Pin this probe even if an image tag changes.
            image_env = info["Config"].get("Env", [])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
            return None
        name = "clawtune-rule-help-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--pull=never", "--name", name,
                   "--network=none", "--read-only", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m",
                   "--cpus=1", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", "--entrypoint", argv[0]]
        if context.workspace:
            command += ["--mount", f"type=bind,src={context.host_workspace},dst={context.workspace},readonly",
                        "--workdir", context.cwd or context.workspace]
        for variable in image_env:
            key = variable.split("=", 1)[0]
            if key not in {"PATH", "HOME", "LANG", "LC_ALL"}:
                command += ["--env", key + "="]
        command += [image, *argv[1:]]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
        output = bytearray()
        overflow = threading.Event()
        def read():
            while chunk := process.stdout.read(4096):
                if len(output) + len(chunk) > LIMIT:
                    overflow.set()
                    process.kill()
                    return
                output.extend(chunk)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            process.wait(timeout=3)
            reader.join(timeout=1)
            if process.returncode != 0 or overflow.is_set() or reader.is_alive() or not output:
                return None
            return record("help", argv[0], output.decode("utf-8", errors="replace"), context.image)
        except subprocess.TimeoutExpired:
            return None
        finally:
            if process.poll() is None:
                process.kill()
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False, env=environment)
            process.wait(timeout=5)
            reader.join(timeout=1)
