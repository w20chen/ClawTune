from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from .bootstrap import ROOT
from .exec_control import (
    ABORT_SCRIPT,
    GATEWAY_ID,
    GATE_CONTAINER_PATH,
    GATE_SCRIPT,
    ExecutionStartRejected,
    SidecarExecutions,
    SidecarUnavailable,
    parse_gate_identity,
)
from swe_rebench.cancellation import run_command, TaskCancelled


class _GateDegrade(RuntimeError):
    """The in-container gate could not provide a trustable process identity."""


class TerminalCaseBuildFailure(RuntimeError):
    """Compose startup failed; the constructor must remove its project."""


def ensure_bfcl():
    # BFCL creates result/score/lock directories at import time. Keep these
    # out of the external source checkout, including during --dry-run.
    os.environ["BFCL_PROJECT_ROOT"] = str(ROOT / ".runtime" / "bfcl")
    # Imported source modules also write bytecode by default. Redirect it
    # before any native import instead of touching the reference checkout.
    sys.pycache_prefix = str(ROOT / ".runtime" / "bfcl" / "pycache")
    root = os.getenv("BFCL_REPO_PATH")
    if root:
        package = Path(root).expanduser().resolve()
        if not (package / "bfcl_eval").is_dir():
            package = package / "berkeley-function-call-leaderboard"
        if not (package / "bfcl_eval").is_dir():
            raise ValueError(f"BFCL package missing: {package}")
        if str(package) not in sys.path:
            sys.path.insert(0, str(package))


class _BFCLImplementation:
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

    def call(self, name: str, arguments: dict, *, call_id: str = ""):
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


def _bfcl_worker(connection, task, run_dir, factory):
    backend = None
    if sys.platform == "linux":
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError("cannot establish BFCL subreaper")
        def terminate(_signal, _frame):
            raise SystemExit(143)
        signal.signal(signal.SIGTERM, terminate)
    try:
        backend = factory(task, run_dir)
        connection.send((True, {k: getattr(backend, k) for k in ("tools", "system", "turns")}))
        while True:
            request = connection.recv()
            if request is None:
                backend.close()
                connection.send((True, None))
                return
            try:
                name, arguments, call_id = request
                connection.send((True, backend.call(name, arguments, call_id=call_id)))
            except Exception as exc:
                connection.send((False, str(exc)))
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        connection.send((False, str(exc)))
    finally:
        connection.close()
        if sys.platform == "linux":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            from swe_rebench.process_supervisor import cleanup
            try:
                clean = cleanup()
            except Exception:
                clean = False
            if not clean:
                raise SystemExit(125)


class BFCLBackend:
    """Keep BFCL's state in one killable worker, not a daemon HTTP thread."""
    def __init__(self, task, run_dir, *, _factory=_BFCLImplementation):
        import multiprocessing
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._cancelled = threading.Event()
        self._worker = context.Process(target=_bfcl_worker, args=(child, task, run_dir, _factory))
        self._worker.start()
        child.close()
        try:
            for key, value in self._receive(timeout=60).items():
                setattr(self, key, value)
        except BaseException:
            self._stop()
            raise

    def _stop(self):
        if self._worker.is_alive():
            self._worker.terminate()
        self._worker.join(timeout=8)
        if self._worker.is_alive():
            self._worker.kill()
            self._worker.join(timeout=5)
            raise RuntimeError("BFCL worker required forced termination; cleanup unconfirmed")
        if self._worker.is_alive():
            raise RuntimeError("BFCL worker did not stop")
        if self._worker.exitcode == 125:
            raise RuntimeError("BFCL descendants did not stop")

    def _receive(self, timeout=300):
        deadline = time.monotonic() + timeout
        while not self._connection.poll(0.1):
            if self._cancelled.is_set() or time.monotonic() >= deadline:
                self._stop()
                raise TaskCancelled("BFCL call cancelled or timed out")
            if not self._worker.is_alive():
                raise RuntimeError("BFCL worker exited without a result")
        ok, value = self._connection.recv()
        if not ok:
            raise RuntimeError(value)
        return value

    def call(self, name, arguments, *, call_id=""):
        if self._cancelled.is_set():
            raise TaskCancelled("BFCL call cancelled")
        self._connection.send((name, arguments, call_id))
        return self._receive()

    def cancel(self):
        self._cancelled.set()

    def close(self):
        try:
            if self._worker.is_alive():
                self._connection.send(None)
                # Normal completion still flushes persistent BFCL memory.
                if self._connection.poll(5):
                    self._connection.recv()
                    self._worker.join(timeout=6)
        finally:
            self._stop()
            self._connection.close()

    def quiesce(self):
        self.close()


class TerminalBackend:
    """Task-owned Compose environment. No transplant into a generic SWE image."""
    tools = [{"name": "terminal_exec", "description": "Run a shell command inside this Terminal Bench task's client container. Use explicit cd when needed.",
              "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}}]

    def __init__(self, task, run_dir: Path, *, deadline: float | None = None, platform: str = "",
                 sidecar_port: int | None = None, runtime_id: str = "",
                 gateway_id: str = GATEWAY_ID, repo: str = "terminal-bench",
                 telemetry_required: bool = False, build_timeout_seconds: float = 1800):
        self.deadline = deadline
        self._cancelled = threading.Event()
        self.telemetry_required = telemetry_required
        # Sidecar execution lifecycle.  Without a port the backend keeps the
        # legacy plain `docker exec` path (no PMU evidence for those calls).
        self.runtime_id = runtime_id
        self.gateway_id = gateway_id or GATEWAY_ID
        self.repo = repo
        self.sidecar = SidecarExecutions(sidecar_port) if sidecar_port else None
        self.gate_available = False
        self.gate_install_error: str | None = None
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
        from .compose import compose_argv
        self.command = [*compose_argv(timeout=self._remaining(30)), "-p", self.project, "-f", str(compose)]
        self.env = dict(os.environ)
        logs = run_dir / "terminal-logs" / task.directory_name
        self.log_dir = logs
        logs.mkdir(parents=True)
        (logs / "agent").mkdir()
        self.env.update({"T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": self.project + "-image",
            "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": self.project + "-client",
            "T_BENCH_TASK_DOCKER_NAME_PREFIX": self.project,
            "T_BENCH_TASK_LOGS_PATH": str(logs), "T_BENCH_TASK_AGENT_LOGS_PATH": str(logs / "agent"),
            "T_BENCH_CONTAINER_LOGS_PATH": "/logs", "T_BENCH_CONTAINER_AGENT_LOGS_PATH": "/agent-logs",
            "T_BENCH_TEST_DIR": "/tests"})
        self.agent_timeout = float(task.payload["config"].get("max_agent_timeout_sec", 360))
        self.timeout = 300
        self.turns = [task.prompt]
        self.system = []
        self.started = False
        # Validate the resolved project before starting it; never run compose in
        # the read-only dataset directory or reuse a user's compose project.
        try:
            resolved = json.loads(self._run(["config", "--format", "json"], timeout=30).stdout)
            # Compose's legacy builder does not honor DOCKER_DEFAULT_PLATFORM.
            # Apply the configured default explicitly, preserving task overrides.
            defaults = {name: {"platform": platform} for name, service in resolved.get("services", {}).items()
                        if platform and not service.get("platform")}
            if defaults:
                platform_file = logs / "compose-platform.json"
                platform_file.write_text(json.dumps({"services": defaults}), encoding="utf-8")
                self.command.extend(["-f", str(platform_file)])
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
            self._run(["up", "-d", "--build"], timeout=build_timeout_seconds)
            self.container = self._run(["ps", "-q", "client"], timeout=30).stdout.strip()
            if not self.container or "\n" in self.container:
                raise ValueError("Terminal Bench Compose must expose exactly one client container")
        except BaseException:
            self.close()
            raise
        if self.sidecar is not None:
            # Both steps must exist before the first tool call; each degrades
            # explicitly instead of failing the task on an exotic image.
            self._install_exec_gate()
            self._register_container_scope()

    def start_agent(self):
        """Apply the native whole-agent budget after environment setup."""
        native_deadline = time.monotonic() + self.agent_timeout
        self.deadline = min(self.deadline, native_deadline) if self.deadline is not None else native_deadline
        return self.deadline

    def _remaining(self, timeout):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Terminal Bench task deadline exceeded")
        return timeout

    def _run(self, args, *, timeout, cleanup=False):
        invoke = subprocess.run if cleanup else run_command
        if args[0] in {"up", "down"}:
            # Persist build/startup diagnostics while Compose is running, too.
            log_path = self.log_dir / f"compose-{args[0]}.log"
            with log_path.open("a", encoding="utf-8") as log:
                try:
                    return invoke([*self.command, *args], cwd=self.root, env=self.env,
                                  text=True, stdout=log, stderr=subprocess.STDOUT, check=True,
                                  timeout=timeout if cleanup else self._remaining(timeout))
                except subprocess.CalledProcessError as exc:
                    error = f"Terminal Compose {args[0]} failed (exit {exc.returncode}); see {log_path}"
                    if args[0] == "up":
                        raise TerminalCaseBuildFailure(error) from exc
                    raise RuntimeError(error) from exc
                except subprocess.TimeoutExpired as exc:
                    if args[0] == "up":
                        raise TerminalCaseBuildFailure(
                            f"Terminal Compose up timed out after {timeout} seconds; see {log_path}"
                        ) from exc
                    raise
        return invoke([*self.command, *args], cwd=self.root, env=self.env,
                              text=True, capture_output=True, check=True,
                              timeout=timeout if cleanup else self._remaining(timeout))

    def call(self, name: str, arguments: dict, *, call_id: str = ""):
        self._check_cancelled()
        if name != "terminal_exec" or not isinstance(arguments.get("command"), str):
            raise ValueError("terminal_exec requires command")
        command = arguments["command"]
        if self.sidecar is None or not self.gate_available or not call_id:
            return self._result(self._plain_exec(command))
        return self._gated_exec(command, call_id)

    def cancel(self):
        if not hasattr(self, "_cancelled"):
            self._cancelled = threading.Event()
        self._cancelled.set()

    def _check_cancelled(self):
        if getattr(self, "_cancelled", None) is not None and self._cancelled.is_set():
            raise TaskCancelled("terminal execution cancelled")

    def _communicate(self, process, *, timeout, input=None):
        deadline = time.monotonic() + timeout
        while True:
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            try:
                return process.communicate(input=input, timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                input = None

    def _result(self, completed) -> dict:
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-65536:],
            "stderr": completed.stderr[-65536:],
        }

    def _plain_exec(self, command: str):
        # Login profiles execute extra programs outside the requested command,
        # polluting PMU counts and invalidating clause attribution.
        process = subprocess.Popen(
            ["docker", "exec", self.container, "sh", "-c", command],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = self._communicate(process, timeout=self._remaining(self.timeout))
            return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        except BaseException:
            self._terminate(process)
            # Killing the Docker client does not stop its container payload.
            # Without a gate identity the owned container is the cleanup boundary.
            subprocess.run(["docker", "stop", "--time", "1", self.container],
                           check=True, capture_output=True, timeout=10)
            raise

    def _gated_exec(self, command: str, call_id: str) -> dict:
        """Run one command behind the gate with a real execution lifecycle.

        The sidecar arms PMU counters on the verified host PID while the gate
        is still blocked, so the counting window starts exactly at the payload
        exec. A missing identity degrades before releasing any payload. With
        a valid identity, telemetry failure releases the same gated child;
        its exit status and cancellation boundary remain authoritative.
        """

        remaining = self._remaining(self.timeout)
        execution_id = "terminal-" + uuid.uuid4().hex[:24]
        process = self._start_gated_process(command)
        update_token: str | None = None
        identity = None
        try:
            identity = self._read_gate_identity(process, timeout=min(30.0, remaining))
            if identity is None:
                raise _GateDegrade("gate identity unavailable")
            container_pid, namespace_inode, starttime_ticks = identity
            assert self.sidecar is not None
            token = self.sidecar.register(
                execution_id=execution_id,
                runtime_id=self.runtime_id,
                tool_call_id=call_id,
                command=command,
                repo=self.repo,
                gateway_id=self.gateway_id,
            )
            update_token = self.sidecar.claim(
                execution_id, token, launcher_pid=os.getpid()
            )
            self.sidecar.started(
                execution_id,
                update_token,
                launcher_pid=os.getpid(),
                container_pid=container_pid,
                namespace_inode=namespace_inode,
                starttime_ticks=starttime_ticks,
                container_id=self.container,
            )
        except (_GateDegrade, SidecarUnavailable, ExecutionStartRejected) as exc:
            if isinstance(exc, _GateDegrade):
                self._disable_gate(str(exc))
            else:
                self._gate_log(f"execution {execution_id} degraded: {exc}")
            if identity is None:
                self._terminate(process)
                self._report_exit(execution_id, update_token, exit_code=None, term_signal=signal.SIGTERM)
                return self._result(self._plain_exec(command))
            # Keep the same gated payload and its verified cleanup identity.
            # Losing telemetry must neither retry the command nor lose the
            # ability to kill its process tree on a later timeout.
        except BaseException:
            self._terminate(process)
            self._report_exit(execution_id, update_token, exit_code=None, term_signal=signal.SIGTERM)
            raise
        try:
            # communicate owns stdin, including EOF. Closing it manually first
            # makes CPython 3.10-3.12 on POSIX flush a closed stream. Check the
            # budget before releasing; never retry a released payload.
            deadline_budget = self._remaining(self.timeout)
            self._check_cancelled()
            stdout, stderr = self._communicate(process, input="go\n", timeout=deadline_budget)
            exit_code = process.returncode
        except BaseException:
            self._abort_payload(identity)
            self._terminate(process)
            self._report_exit(
                execution_id, update_token, exit_code=None, term_signal=signal.SIGKILL
            )
            raise
        self._report_exit(execution_id, update_token, exit_code=exit_code, term_signal=None)
        return {
            "exit_code": exit_code,
            "stdout": (stdout or "")[-65536:],
            "stderr": (stderr or "")[-65536:],
        }

    def _abort_payload(self, identity: tuple[int, int, int]) -> None:
        try:
            result = subprocess.run(
                ["docker", "exec", self.container, "/bin/sh", "-c", ABORT_SCRIPT,
                 "clawtune-abort", *(str(value) for value in identity)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode:
                self._gate_log(f"payload cleanup failed: {result.stderr[-1000:]}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._gate_log(f"payload cleanup failed: {exc}")

    def _start_gated_process(self, command: str) -> subprocess.Popen:
        # `-i` keeps stdin open for the release token.  The harness closes it
        # once released, so the payload sees a closed stdin like a plain exec.
        return subprocess.Popen(
            [
                "docker", "exec", "-i", self.container,
                "/bin/sh", GATE_CONTAINER_PATH, "/bin/sh", "-c", command,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _read_gate_identity(self, process: subprocess.Popen, *, timeout: float):
        """Read the gate's identity line, killing the gate on timeout."""

        if process.stdout is None:
            return None
        timer = threading.Timer(max(0.1, timeout), process.kill)
        timer.daemon = True
        timer.start()
        try:
            line = process.stdout.readline()
        except (OSError, ValueError):
            return None
        finally:
            timer.cancel()
        if process.returncode is not None:
            return None
        return parse_gate_identity(line)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        # EOF aborts an unreleased in-container gate. Killing only the Docker
        # client can leave the remote shell blocked on its stdin indefinitely.
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    def _report_exit(
        self,
        execution_id: str,
        update_token: str | None,
        *,
        exit_code: int | None,
        term_signal: int | None,
    ) -> None:
        if self.sidecar is None or update_token is None:
            return
        for attempt in range(3):
            try:
                self.sidecar.exited(
                    execution_id, update_token, exit_code=exit_code, term_signal=term_signal
                )
                return
            except SidecarUnavailable as exc:
                self._gate_log(f"execution {execution_id} exit report failed: {exc}")
                if attempt == 2:
                    return
                time.sleep(0.1 * (attempt + 1))

    def _disable_gate(self, reason: str) -> None:
        if self.gate_available:
            self._gate_log(f"exec gate disabled for this task: {reason}")
        self.gate_available = False
        self.gate_install_error = reason

    def _install_exec_gate(self) -> None:
        """Inject the POSIX gate into the client container and probe it once."""

        script = self.log_dir / "clawtune-exec-gate.sh"
        try:
            script.write_text(GATE_SCRIPT, encoding="utf-8", newline="\n")
            copied = run_command(
                ["docker", "cp", str(script), f"{self.container}:{GATE_CONTAINER_PATH}"],
                capture_output=True, text=True, timeout=60,
            )
            if copied.returncode != 0:
                raise RuntimeError((copied.stderr or "").strip() or "docker cp failed")
            probe = run_command(
                [
                    "docker", "exec", "-i", self.container,
                    "/bin/sh", GATE_CONTAINER_PATH, "/bin/sh", "-c", ":",
                ],
                input="go\n", capture_output=True, text=True, timeout=60,
            )
            lines = (probe.stdout or "").splitlines()
            if probe.returncode != 0 or not lines or parse_gate_identity(lines[0]) is None:
                raise RuntimeError(
                    f"gate probe exit={probe.returncode} stdout={probe.stdout!r} "
                    f"stderr={probe.stderr!r}"
                )
        except Exception as exc:  # capability probe: degrade, never fail the task
            self._disable_gate(f"{type(exc).__name__}: {exc}")
            return
        self.gate_available = True
        self._gate_log("exec gate ready")

    def _register_container_scope(self) -> None:
        """Publish the task's client container as this runtime's sampling scope.

        Parallel tasks each own one Compose project, so binding container id,
        host PID and cgroup per runtime keeps container/cgroup/PMU spans
        one-to-one at any parallelism.
        """

        if self.sidecar is None or not self.runtime_id:
            return
        try:
            from swe_rebench.host_openclaw import _docker_container_scope

            scope = _docker_container_scope(
                shutil.which("docker") or "docker", self.container
            )
            if scope is None:
                raise RuntimeError("client container scope could not be derived")
            self.sidecar.store_container_scope(
                self.runtime_id, scope, gateway_id=self.gateway_id
            )
        except Exception as exc:
            self._gate_log(
                f"container scope registration failed: {type(exc).__name__}: {exc}"
            )

    def _gate_log(self, message: str) -> None:
        try:
            with (self.log_dir / "terminal-gate.log").open("a", encoding="utf-8") as log:
                log.write(message.rstrip() + "\n")
        except OSError:
            pass

    def quiesce(self):
        self.cancel()
        if self.started:
            self._run(["stop", "-t", "5"], timeout=30, cleanup=True)

    def close(self):
        if self.started:
            self._run(["down", "--volumes", "--remove-orphans"], timeout=60, cleanup=True)
            self.started = False
        if self.sidecar is not None and self.runtime_id:
            try:
                self.sidecar.delete_container_scope(
                    self.runtime_id, gateway_id=self.gateway_id
                )
            except SidecarUnavailable as exc:
                self._gate_log(f"container scope cleanup failed: {exc}")
