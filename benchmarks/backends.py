from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from .bootstrap import ROOT
from swe_rebench.cancellation import run_command


def ensure_bfcl():
    # BFCL creates result/score/lock directories at import time. Keep these
    # out of the external source checkout, including during --dry-run.
    os.environ["BFCL_PROJECT_ROOT"] = str(ROOT / ".runtime" / "bfcl")
    root = os.getenv("BFCL_REPO_PATH")
    if root:
        package = Path(root).expanduser().resolve()
        if not (package / "bfcl_eval").is_dir():
            package = package / "berkeley-function-call-leaderboard"
        if not (package / "bfcl_eval").is_dir():
            raise ValueError(f"BFCL package missing: {package}")
        if str(package) not in sys.path:
            sys.path.insert(0, str(package))


class BFCLBackend:
    """BFCL owns function schemas and backend state; ClawTune owns execution."""
    def __init__(self, task, run_dir: Path):
        ensure_bfcl()
        try:
            from bfcl_eval.eval_checker.multi_turn_eval.multi_turn_utils import execute_multi_turn_func_call
            from bfcl_eval.model_handler.utils import convert_to_tool
            from bfcl_eval.constants.enums import ModelStyle
            from bfcl_eval.constants.type_mappings import GORILLA_TO_OPENAPI
        except ImportError as exc:
            raise RuntimeError("Install BFCL dependencies and set BFCL_REPO_PATH to the gorilla checkout") from exc
        entry = copy.deepcopy(task.payload["entry"])
        category = task.payload["category"]
        if "memory" in category or entry.get("depends_on") or entry.get("missed_function"):
            raise ValueError("BFCL dependency scheduling and per-turn tool changes are not supported")
        if "web_search" in category:
            if not os.getenv("SERPAPI_API_KEY"):
                raise ValueError("BFCL web_search requires SERPAPI_API_KEY (not TAVILY_API_KEY)")
            from bfcl_eval.utils import populate_initial_settings_for_web_search_test_cases
            populate_initial_settings_for_web_search_test_cases([entry])
        _, self.instances = execute_multi_turn_func_call([], entry.get("initial_config", {}),
            entry["involved_classes"], "clawtune_" + uuid.uuid4().hex, entry["id"],
            long_context="long_context" in category or "composite" in category, is_evaL_run=False)
        self.tools, self.methods = [], {}
        schemas = convert_to_tool(entry["function"], GORILLA_TO_OPENAPI, ModelStyle.OPENAI_COMPLETIONS)
        if len(schemas) != len(entry["function"]):
            raise ValueError("BFCL schema conversion changed function count")
        for original, wrapped in zip(entry["function"], schemas):
            schema = wrapped.get("function", wrapped)
            matches = [getattr(instance, original["name"]) for instance in self.instances.values()
                       if callable(getattr(instance, original["name"], None)) and not original["name"].startswith("_")]
            if len(matches) != 1 or schema["name"] in self.methods:
                raise ValueError(f"BFCL tool has missing or ambiguous implementation: {original['name']}")
            self.tools.append(schema)
            self.methods[schema["name"]] = matches[0]
        self.system, self.turns = [], []
        for turn in entry["question"]:
            if not isinstance(turn, list):
                raise ValueError("BFCL question must contain lists of messages")
            users = []
            for message in turn:
                if message["role"] == "system":
                    self.system.append(str(message["content"]))
                elif message["role"] == "user":
                    users.append(str(message["content"]))
                else:
                    raise ValueError("BFCL supports system/user input turns only")
            if users:
                self.turns.append("\n\n".join(users))
        if not self.turns:
            raise ValueError("BFCL entry contains no user turns")

    def call(self, name: str, arguments: dict):
        if name not in self.methods:
            raise ValueError("unknown BFCL function")
        try:
            return self.methods[name](**arguments)
        except Exception as exc:
            return {"error": str(exc)}

    def close(self):
        for instance in self.instances.values():
            flush = getattr(instance, "_flush_memory_to_local_file", None)
            if callable(flush):
                flush()


class TerminalBackend:
    """Task-owned Compose environment. No transplant into a generic SWE image."""
    tools = [{"name": "terminal_exec", "description": "Run a shell command inside this Terminal Bench task's client container. Use explicit cd when needed.",
              "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}]

    def __init__(self, task, run_dir: Path, *, deadline: float | None = None):
        self.deadline = deadline
        self.project = "ct-" + uuid.uuid4().hex[:16]
        self.root = run_dir / "terminal-environments" / task.directory_name
        shutil.copytree(task.payload["task_path"], self.root)
        compose = next((self.root / name for name in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml") if (self.root / name).exists()), None)
        if compose is None:
            if not (self.root / "Dockerfile").is_file():
                raise ValueError("Terminal Bench task requires a Dockerfile or Compose file")
            # v1 tasks may rely on the harness's default single client
            # Compose service. Preserve their own Dockerfile/build context.
            import yaml
            compose = self.root / "docker-compose.yaml"
            compose.write_text(yaml.safe_dump({"services": {"client": {
                "build": {"context": ".", "dockerfile": "Dockerfile"},
                "image": "${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}",
                "container_name": "${T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME}",
                "command": ["sh", "-c", "sleep infinity"],
                "environment": {"TEST_DIR": "${T_BENCH_TEST_DIR}"},
                "volumes": ["${T_BENCH_TASK_LOGS_PATH}:${T_BENCH_CONTAINER_LOGS_PATH}",
                            "${T_BENCH_TASK_AGENT_LOGS_PATH}:${T_BENCH_CONTAINER_AGENT_LOGS_PATH}"],
            }}}), encoding="utf-8")
        self.command = ["docker", "compose", "-p", self.project, "-f", str(compose)]
        self.env = dict(os.environ)
        logs = run_dir / "terminal-logs" / task.directory_name
        logs.mkdir(parents=True)
        (logs / "agent").mkdir()
        self.env.update({"T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": self.project + "-image",
            "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": self.project + "-client",
            "T_BENCH_TASK_DOCKER_NAME_PREFIX": self.project,
            "T_BENCH_TASK_LOGS_PATH": str(logs), "T_BENCH_TASK_AGENT_LOGS_PATH": str(logs / "agent"),
            "T_BENCH_CONTAINER_LOGS_PATH": "/logs", "T_BENCH_CONTAINER_AGENT_LOGS_PATH": "/agent-logs",
            "T_BENCH_TEST_DIR": "/tests"})
        self.timeout = min(300, float(task.payload["config"].get("max_agent_timeout_sec", 300)))
        self.turns = [task.prompt]
        self.system = []
        self.started = False
        # Validate the resolved project before starting it; never run compose in
        # the read-only dataset directory or reuse a user's compose project.
        try:
            resolved = json.loads(self._run(["config", "--format", "json"], timeout=30).stdout)
            for service in resolved.get("services", {}).values():
                for mount in service.get("volumes", []):
                    if mount.get("type") == "bind":
                        source = Path(mount["source"]).resolve()
                        if not source.is_relative_to(run_dir.resolve()):
                            raise ValueError(f"Terminal task bind is outside its run directory: {source}")
                build = service.get("build")
                if isinstance(build, dict) and not Path(build["context"]).resolve().is_relative_to(self.root.resolve()):
                    raise ValueError("Terminal task build context is outside its copied task")
            self.started = True
            self._run(["up", "-d", "--build"], timeout=600)
            self.container = self._run(["ps", "-q", "client"], timeout=30).stdout.strip()
            if not self.container or "\n" in self.container:
                raise ValueError("Terminal Bench Compose must expose exactly one client container")
        except BaseException:
            self.close()
            raise

    def _remaining(self, timeout):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Terminal Bench task deadline exceeded")
        return timeout

    def _run(self, args, *, timeout, cleanup=False):
        invoke = subprocess.run if cleanup else run_command
        return invoke([*self.command, *args], cwd=self.root, env=self.env,
                              text=True, capture_output=True, check=True,
                              timeout=timeout if cleanup else self._remaining(timeout))

    def call(self, name: str, arguments: dict):
        if name != "terminal_exec" or not isinstance(arguments.get("command"), str):
            raise ValueError("terminal_exec requires command")
        completed = subprocess.run(["docker", "exec", self.container, "sh", "-lc", arguments["command"]],
                                   text=True, capture_output=True, timeout=self._remaining(self.timeout))
        return {"exit_code": completed.returncode, "stdout": completed.stdout[-65536:], "stderr": completed.stderr[-65536:]}

    def close(self):
        if self.started:
            self._run(["down", "--volumes", "--remove-orphans"], timeout=60, cleanup=True)
            self.started = False
