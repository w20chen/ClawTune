"""Replay-only compatibility tools carried by the existing plugin tool bridge."""
from __future__ import annotations

import json
from pathlib import Path

from swe_rebench.cancellation import Cancellation, cancellation_scope, run_command


class LegacyReplayTools:
    def __init__(self, *, workspace: Path, deadline: float | None, names: set[str]):
        from swe_rebench.host_openclaw import _sandbox_container_prefix, _require_executable
        self.prefix = _sandbox_container_prefix(workspace)
        self.docker = _require_executable("docker")
        self.deadline = deadline
        self.cancellation = Cancellation()
        self.errors: list[str] = []
        root = Path(__file__).resolve().parents[1]
        schemas = json.loads((root / "contracts/replay-legacy-tools.schema.json").read_text(encoding="utf-8"))["$defs"]
        self.tools = [{"name": name, "description": "Replay recorded legacy tool in the task sandbox",
                       "parameters": schemas[name]} for name in sorted(names & schemas.keys())]
        self.names = {tool["name"] for tool in self.tools}
        self.helper = Path(__file__).with_name("replay_files.py").read_text(encoding="utf-8")

    def call(self, name: str, arguments: dict, *, call_id: str = ""):
        try:
            return self._call(name, arguments)
        except Exception as exc:
            self.errors.append(f"{call_id}: {exc}")
            raise

    def _call(self, name: str, arguments: dict):
        from swe_rebench.host_openclaw import _docker_sandbox_container_ids, _remaining_task_seconds
        if name not in self.names:
            raise ValueError(f"unknown replay compatibility tool: {name}")
        if name == "clawtune_replay_message":
            # The historical CLI message is local output, never a live message
            # through OpenClaw's native external-channel `message` tool.
            return str(arguments.get("content", ""))
        containers = _docker_sandbox_container_ids(self.docker, self.prefix)
        if len(containers) != 1:
            raise RuntimeError("legacy replay requires exactly one active task sandbox")
        remaining = _remaining_task_seconds(self.deadline, phase="legacy replay file tool")
        with cancellation_scope(self.cancellation):
            result = run_command(
                [self.docker, "exec", "-i", "--workdir", "/testbed", containers[0], "python3", "-c", self.helper],
                input=json.dumps({"name": name, "arguments": arguments}),
                text=True, encoding="utf-8", capture_output=True, timeout=remaining,
            )
        if result.returncode:
            raise RuntimeError(f"legacy replay tool failed: {result.stderr[-2000:]}")
        return json.loads(result.stdout)

    def cancel(self):
        self.cancellation.cancel()
