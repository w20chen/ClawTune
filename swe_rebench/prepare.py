"""
Runtime bundle preparation.

Assembles a self-contained directory that is volume-mounted into every
swe-rebench container at ``/claw``.  The bundle includes:

- The OpenClaw plugin source (``plugin/``)
- The scheduler sidecar source (``scheduler/``)
- Generated entrypoint and setup scripts
- Generated OpenClaw plugin config (pointing at localhost:8765)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from swe_rebench.config import RunnerConfig

_BUNDLE_FINGERPRINT_FILE = "bundle-source-fingerprint.json"


_PLUGIN_CONFIG: dict[str, Any] = {
    "endpoint": "http://127.0.0.1:8765",
    "mode": "observe",
    "decisionTimeoutMs": 800,
    # Starting/finalizing a healthy BCC collector can block the in-container
    # sidecar for several seconds.  Keep lifecycle/scope/telemetry requests
    # alive across that bounded work instead of dropping the first concurrent
    # calls with the plugin's generic 800 ms default.
    "reportTimeoutMs": 10000,
    "failOpen": True,
    "sendRawParams": False,
    "recordRawTrace": False,
    "logLevel": "info",
    "consoleMode": "verbose",
    "executionBackend": "managed-wrapper",
    "launcherPath": "/opt/claw/bin/claw-launch",
    # Run the shell wrapper through an explicit interpreter.  Recent OpenClaw
    # exec transports special-case direct script paths, which is unsuitable for
    # the managed launcher lifecycle.
    "launcherInterpreter": "/bin/sh",
    "instrumentHosts": ["gateway"],
    "instrumentTools": ["exec"],
    "enableCgroup": True,
    "enableAffinity": False,
    "enableNuma": False,
    "profilingMode": "off",
    "securityBoundaryAccepted": True,
    # Entrypoint starts the sidecar before OpenClaw.  Keep auto-start
    # disabled so the plugin never tries to spawn a second sidecar in
    # the container.
    "autoStartSidecar": False,
    "trace": {
        "schema_version": 6,
        "include_raw_events": False,
        "include_llm_messages": True,
        "include_tool_outputs": True,
        "redact_sensitive_data": True,
        "flush_span_start": True,
        "max_string_bytes": 16384,
        "max_messages_bytes": 131072,
        "max_tool_output_bytes": 65536,
        "trace_dir": "",
    },
}

# ── Shared bash snippet: detect correct python/pip for swe-rebench images ──
# swe-rebench images ship conda python at /opt/conda/bin/python3.
# We prefer it over any system python for package consistency.
_BASH_PYTHON_DETECT = '''
# Detect python: prefer conda python shipped by swe-rebench images.
if [ -x /opt/conda/bin/python3 ]; then
    _CLW_PYTHON="/opt/conda/bin/python3"
    _CLW_PIP="/opt/conda/bin/pip"
elif command -v python3 &>/dev/null; then
    _CLW_PYTHON="$(command -v python3)"
    _CLW_PIP="$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null)"
else
    _CLW_PYTHON="python3"
    _CLW_PIP="pip3"
fi
'''


def build_bundle(config: RunnerConfig) -> Path:
    repo = config.repo_root
    bundle_dir = repo / config.bundle.output_dir
    if bundle_dir.exists():
        _remove_tree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    _build_plugin_dist(repo, bundle_dir, config)
    _copy_plugin(repo, bundle_dir, config)
    _copy_scheduler(repo, bundle_dir, config)
    _write_entrypoint(bundle_dir, config)
    _write_setup_script(bundle_dir)
    _write_plugin_config(bundle_dir)
    _write_run_agent(bundle_dir, config)
    _write_bundle_fingerprint(config, bundle_dir)

    _log(f"Bundle assembled at {bundle_dir}")
    _log(f"  plugin/     <- {repo / config.bundle.plugin_source}")
    _log(f"  scheduler/  <- {repo / config.bundle.scheduler_source}")
    return bundle_dir


def bundle_needs_rebuild(config: RunnerConfig, bundle_dir: Path | None = None) -> bool:
    repo = config.repo_root
    bundle = bundle_dir or repo / config.bundle.output_dir
    marker = bundle / "entrypoint.sh"
    if not marker.exists():
        return True
    fingerprint_path = bundle / _BUNDLE_FINGERPRINT_FILE
    if not fingerprint_path.exists():
        return True
    try:
        previous = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    current = _bundle_source_fingerprint(config)
    if previous.get("digest") != current["digest"]:
        return True

    marker_mtime = marker.stat().st_mtime
    sources = [
        repo / config.bundle.plugin_source,
        repo / config.bundle.scheduler_source,
        Path(__file__).resolve(),
    ]
    for source in sources:
        if source.exists() and _latest_source_mtime(source) > marker_mtime:
            return True
    return False


def _write_bundle_fingerprint(config: RunnerConfig, bundle_dir: Path) -> None:
    path = bundle_dir / _BUNDLE_FINGERPRINT_FILE
    path.write_text(
        json.dumps(_bundle_source_fingerprint(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _bundle_source_fingerprint(config: RunnerConfig) -> dict[str, Any]:
    repo = config.repo_root
    roots = [
        repo / config.bundle.plugin_source,
        repo / config.bundle.scheduler_source,
        repo / "contracts",
        repo / "traces" / "tool-resource",
        Path(__file__).resolve(),
        repo / "swe_rebench" / "runner.py",
        repo / "swe_rebench" / "host_sandbox.py",
    ]
    files = _fingerprint_files(roots)
    digest = hashlib.sha256()
    entries: list[str] = []
    for path in files:
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError:
            relative = path.as_posix()
        entries.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return {
        "schema": "swe_rebench_bundle_source_fingerprint_v1",
        "digest": f"sha256:{digest.hexdigest()}",
        "file_count": len(entries),
        "files": entries,
    }


def _fingerprint_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    skip_names = {
        ".git",
        ".mypy_cache",
        ".npm-cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "dist",
        "node_modules",
        "traces",
    }
    skip_suffixes = {".pyc", ".pyo", ".whl", ".tar.gz"}
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            files.append(root)
            continue
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            if any(part in skip_names for part in item.parts):
                continue
            if any(item.name.endswith(suffix) for suffix in skip_suffixes):
                continue
            files.append(item)
    return sorted(set(files), key=lambda path: path.as_posix())


def _build_plugin_dist(repo: Path, bundle_dir: Path, config: RunnerConfig) -> None:
    """Rebuild ignored plugin JS so git resets cannot leave stale runtime code."""
    plugin_dir = repo / config.bundle.plugin_source
    package_json = plugin_dir / "package.json"
    if not package_json.exists():
        raise FileNotFoundError(f"plugin package.json not found: {package_json}")

    dist_dir = plugin_dir / "dist"
    if dist_dir.exists():
        _remove_tree(dist_dir)

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise FileNotFoundError("required executable not found: npm")

    log_path = bundle_dir / "plugin-build.log"
    result: subprocess.CompletedProcess[str] | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                [npm, "run", "build"],
                cwd=str(plugin_dir),
                stdout=log,
                stderr=log,
                text=True,
            )
    finally:
        # Strict host runs invoke prepare through sudo for eBPF access. npm/tsc
        # then creates ignored source-tree dist files as root unless ownership
        # is restored to the original caller. Do this even after a failed build
        # so the next normal-user `npm run build` can repair partial output.
        _restore_sudo_user_ownership(dist_dir)
    if result is None:
        raise RuntimeError("plugin_build_failed before npm returned a result")
    if result.returncode != 0:
        raise RuntimeError(
            f"plugin_build_failed exit={result.returncode}: "
            f"{_tail_text(log_path, 2000)}"
        )
    _log("  Rebuilt plugin dist")


def _restore_sudo_user_ownership(path: Path) -> None:
    """Return a generated tree to the non-root user who invoked sudo."""

    if not path.exists() or not hasattr(os, "geteuid") or not hasattr(os, "chown"):
        return
    if os.geteuid() != 0:
        return
    try:
        uid = int(os.environ["SUDO_UID"])
        gid = int(os.environ["SUDO_GID"])
    except (KeyError, TypeError, ValueError):
        return
    if uid <= 0 or gid < 0:
        return

    entries = [path, *path.rglob("*")]
    # Children first avoids removing directory traversal permission before all
    # generated files have been visited.
    for entry in reversed(entries):
        try:
            os.chown(entry, uid, gid, follow_symlinks=False)
        except FileNotFoundError:
            continue


def _latest_source_mtime(path: Path) -> float:
    if path.is_file():
        return path.stat().st_mtime
    latest = path.stat().st_mtime
    skip = {"node_modules", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "dist"}
    for item in path.rglob("*"):
        if any(part in skip for part in item.parts):
            continue
        try:
            mtime = item.stat().st_mtime
        except OSError:
            continue
        latest = max(latest, mtime)
    return latest


def _copy_plugin(repo: Path, bundle_dir: Path, config: RunnerConfig) -> None:
    src = repo / config.bundle.plugin_source
    dst = bundle_dir / "plugin"
    _copytree_selective(src, dst, skip={"node_modules", ".npm-cache", ".git", "__pycache__"})
    _log(f"  Copied plugin source ({_count_files(dst)} files)")


def _copy_scheduler(repo: Path, bundle_dir: Path, config: RunnerConfig) -> None:
    src = repo / config.bundle.scheduler_source
    dst = bundle_dir / "scheduler"
    _copytree_selective(src, dst, skip={
        "__pycache__", ".pytest_cache", ".pytest-tmp*", "*.egg-info", "traces",
        "scheduler.sqlite3*", "dist", "*.whl", "*.tar.gz",
    })
    _log(f"  Copied scheduler source ({_count_files(dst)} files)")


# ══════════════════════════════════════════════════════════════════
#  entrypoint.sh
# ══════════════════════════════════════════════════════════════════

_ENTRYPOINT_TEMPLATE = r"""#!/bin/bash
set -euo pipefail
CLAW_ROOT="/claw"
TRACE_DIR="/traces"
SIDECAR_PORT=8765
""" + _BASH_PYTHON_DETECT + r"""

echo "[claw] === Phase 1: environment setup ==="
bash "$CLAW_ROOT/setup.sh"

echo "[claw] === Phase 2: start sidecar ==="
if [ -z "${LLM_API_KEY:-}" ]; then
    echo "[claw] FATAL: LLM_API_KEY is not set; refusing to run with the local sk-test placeholder"
    mkdir -p "$TRACE_DIR"
    cat > "$TRACE_DIR/result_summary.json" <<EOF
{
  "task_id": "${TASK_INSTANCE_ID:-}",
  "agent_exit_code": 2,
  "testbed_exists": $([ -d /testbed ] && echo true || echo false),
  "patch_bytes": 0,
  "has_patch": false,
  "error": "LLM_API_KEY is not set"
}
EOF
    exit 2
fi
export AGENT_SCHEDULER_DB_PATH="/tmp/scheduler.sqlite3"
export AGENT_SCHEDULER_TRACE_DIR="$TRACE_DIR"
export AGENT_SCHEDULER_DOCKER_EXEC_OBSERVER="${AGENT_SCHEDULER_DOCKER_EXEC_OBSERVER:-true}"
export AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED="${AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED:-false}"
export AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL="${LLM_UPSTREAM_BASE_URL:-__UPSTREAM__}"
export AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY="${LLM_API_KEY:-__LLM_KEY__}"
export AGENT_SCHEDULER_LLM_PROXY_ENABLED="true"
# Model spoofing: the sidecar auto-normalises upstream /v1/models by default.
# Setting both vars explicitly provides a synthetic fallback for cases where
# the upstream /models endpoint is unreachable or returns unparseable data.
# Set UPSTREAM_MODEL to a different value to translate model names.
export AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL="${LLM_MODEL:-__MODEL_SHORT__}"
export AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL="${LLM_MODEL:-__MODEL_SHORT__}"
export AGENT_SCHEDULER_POLICY="observe-only"
export CLAW_SCHEDULER_ENDPOINT="http://127.0.0.1:$SIDECAR_PORT"
export OPENCLAW_SCHEDULER_ENDPOINT="$CLAW_SCHEDULER_ENDPOINT"
export CLAW_EXEC_WORKDIR="/testbed"
export OPENCLAW_WORKSPACE_DIR="/testbed"
export OPENCLAW_REPO_ROOT="/testbed"
# Keep managed exec payloads in the task image's Python environment.  The
# sidecar itself deliberately uses _CLW_PYTHON, but bare python3/pip issued by
# an agent must resolve to the same task interpreter instead of /usr/bin.
if [ -x /opt/miniconda3/envs/testbed/bin/python3 ]; then
    export CLAW_TASK_PYTHON="/opt/miniconda3/envs/testbed/bin/python3"
elif [ -x /opt/conda/envs/testbed/bin/python3 ]; then
    export CLAW_TASK_PYTHON="/opt/conda/envs/testbed/bin/python3"
else
    export CLAW_TASK_PYTHON="$_CLW_PYTHON"
fi
export PATH="/opt/claw/bin:$(dirname "$CLAW_TASK_PYTHON"):$PATH"
CONTAINER_ID_CANDIDATE="$(hostname 2>/dev/null || true)"
if [ -n "$CONTAINER_ID_CANDIDATE" ]; then
    export AGENT_SCHEDULER_SANDBOX_CONTAINER_ID="${AGENT_SCHEDULER_SANDBOX_CONTAINER_ID:-$CONTAINER_ID_CANDIDATE}"
    export CLAW_SANDBOX_CONTAINER_ID="${CLAW_SANDBOX_CONTAINER_ID:-$CONTAINER_ID_CANDIDATE}"
fi
CLAW_BCC_PYTHONPATH=""
if [ -s /tmp/.claw_bcc_pythonpath ]; then
    CLAW_BCC_PYTHONPATH="$(cat /tmp/.claw_bcc_pythonpath)"
fi
CLAW_BCC_LD_PRELOAD=""
# Keep the ABI repair out of the entrypoint environment.  This array is passed
# only to the BCC preflight and sidecar process below, never to task payloads.
CLAW_BCC_RUNTIME_ENV=()
if [ -s /tmp/.claw_bcc_ld_preload ]; then
    IFS= read -r CLAW_BCC_LD_PRELOAD < /tmp/.claw_bcc_ld_preload \
        || CLAW_BCC_LD_PRELOAD=""
    case "$CLAW_BCC_LD_PRELOAD" in
        /lib/*|/lib64/*|/usr/lib/*|/usr/lib64/*)
            if [ -r "$CLAW_BCC_LD_PRELOAD" ]; then
                CLAW_BCC_RUNTIME_ENV=(
                    "LD_PRELOAD=${CLAW_BCC_LD_PRELOAD}${LD_PRELOAD:+:$LD_PRELOAD}"
                )
            fi
            ;;
    esac
fi
mkdir -p "$TRACE_DIR"
$_CLW_PYTHON - <<'PY' > "$TRACE_DIR/cgroup_probe.json" 2>/dev/null || true
import json
import os
from pathlib import Path

root = Path(os.environ.get("CLAW_CGROUP_ROOT") or "/sys/fs/cgroup/claw")
mount = Path("/sys/fs/cgroup")
self_path = None
try:
    for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            value = line[3:]
            self_path = "/sys/fs/cgroup" if value in {"", "/"} else f"/sys/fs/cgroup{value}"
            break
except OSError:
    pass
probe = {
    "cgroup_required": os.environ.get("CLAW_CGROUP_REQUIRED") == "1",
    "cgroup_root": str(root),
    "cgroup_root_exists": root.exists(),
    "cgroup_root_parent_exists": root.parent.exists(),
    "cgroup_root_parent_writable": os.access(root.parent, os.W_OK),
    "cgroup_mount_exists": mount.exists(),
    "cgroup_mount_writable": os.access(mount, os.W_OK),
    "self_cgroup_path": self_path,
    "container_id": os.environ.get("AGENT_SCHEDULER_SANDBOX_CONTAINER_ID"),
}
print(json.dumps(probe, indent=2))
PY

env "${CLAW_BCC_RUNTIME_ENV[@]}" \
    "PYTHONPATH=$CLAW_ROOT/scheduler/src${CLAW_BCC_PYTHONPATH:+:$CLAW_BCC_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}" \
    "$_CLW_PYTHON" - <<'PY' > "$TRACE_DIR/tool_resource_preflight.json" 2>&1 || true
import json
import http.client
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

container_id = os.environ.get("AGENT_SCHEDULER_SANDBOX_CONTAINER_ID") or os.environ.get("CLAW_SANDBOX_CONTAINER_ID")
docker = shutil.which("docker")
docker_inspect = {"ok": False, "detail": "docker or container id unavailable"}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=1.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        connected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connected.settimeout(self.timeout)
        connected.connect(self.socket_path)
        self.sock = connected


def _inspect_from_socket(value):
    socket_path = os.environ.get("AGENT_SCHEDULER_DOCKER_SOCKET", "/var/run/docker.sock")
    if not os.path.exists(socket_path):
        return None
    connection = _UnixHTTPConnection(socket_path)
    try:
        connection.request("GET", f"/containers/{value}/json")
        response = connection.getresponse()
        payload = response.read()
    except OSError:
        return None
    finally:
        connection.close()
    if response.status != 200:
        return None
    try:
        document = json.loads(payload.decode("utf-8"))
        pid = document.get("State", {}).get("Pid")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    return pid if isinstance(pid, int) and pid > 0 else None


if docker and container_id:
    result = subprocess.run(
        [docker, "inspect", container_id, "--format", "{{.State.Pid}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    docker_inspect = {
        "ok": result.returncode == 0 and result.stdout.strip().isdigit(),
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "returncode": result.returncode,
    }
if container_id and not docker_inspect.get("ok"):
    socket_pid = _inspect_from_socket(container_id)
    if socket_pid is not None:
        docker_inspect = {
            "ok": True,
            "pid": socket_pid,
            "source": "docker-unix-socket",
        }
try:
    import bcc  # noqa: F401
    bcc_import = {"ok": True, "error": None}
except Exception as exc:
    bcc_import = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
try:
    from tool_resource import mvdan_client

    binary_path = mvdan_client.default_binary_path()
    builder_path = (
        Path(mvdan_client.__file__).with_name("_mvdan_adapter") / "build.sh"
    )
    status_path = Path("/tmp/.claw_mvdan_adapter_status.json")
    try:
        provision_status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        provision_status = None
    mvdan_adapter = {
        "ok": False,
        "binary_path": str(binary_path),
        "binary_exists": binary_path.is_file(),
        "binary_executable": os.access(binary_path, os.X_OK),
        "parser": {
            "name": mvdan_client.PARSER_NAME,
            "version": mvdan_client.PARSER_VERSION,
        },
        "protocol": {
            "version": mvdan_client.ADAPTER_PROTOCOL_VERSION,
            "required_capabilities": sorted(
                mvdan_client.REQUIRED_CAPABILITIES
            ),
        },
        "builder_path": str(builder_path),
        "builder_exists": builder_path.is_file(),
        "builder_mode": (
            oct(builder_path.stat().st_mode & 0o777)
            if builder_path.exists()
            else None
        ),
        "provision_status": provision_status,
        "error": None,
    }
    with mvdan_client.MvdanClient(binary_path):
        pass
    mvdan_adapter["ok"] = True
except Exception as exc:
    if "mvdan_adapter" not in locals():
        mvdan_adapter = {"ok": False}
    mvdan_adapter["error"] = f"{type(exc).__name__}: {exc}"
tracefs = {"path": None, "sched_process_exit": False, "kprobe_events_writable": False}
for candidate in (Path("/sys/kernel/tracing"), Path("/sys/kernel/debug/tracing")):
    tracepoint_id = candidate / "events/sched/sched_process_exit/id"
    try:
        if not tracepoint_id.is_file():
            continue
        tracefs = {
            "path": str(candidate),
            "sched_process_exit": True,
            "kprobe_events_writable": os.access(candidate / "kprobe_events", os.W_OK),
        }
        break
    except OSError:
        continue
preflight = {
    "platform": platform.system().lower(),
    "euid": os.geteuid() if hasattr(os, "geteuid") else None,
    "python": sys.executable,
    "pythonpath": os.environ.get("PYTHONPATH", ""),
    "bcc_ld_preload": os.environ.get("LD_PRELOAD"),
    "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
    "docker": docker,
    "clang": shutil.which("clang"),
    "llc": shutil.which("llc"),
    "bpftool": shutil.which("bpftool"),
    "container_id": container_id,
    "docker_inspect": docker_inspect,
    "bcc_import": bcc_import,
    "mvdan_adapter": mvdan_adapter,
    "tracefs": tracefs,
    "stage2_ready": (
        platform.system().lower() == "linux"
        and (not hasattr(os, "geteuid") or os.geteuid() == 0)
        and Path("/sys/fs/cgroup/cgroup.controllers").is_file()
        and docker_inspect.get("ok") is True
        and bcc_import.get("ok") is True
        and mvdan_adapter.get("ok") is True
        and tracefs.get("sched_process_exit") is True
        and tracefs.get("kprobe_events_writable") is True
    ),
}
print(json.dumps(preflight, indent=2))
PY

case "${AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED,,}" in
    1|true|yes|on)
        if ! "$_CLW_PYTHON" - "$TRACE_DIR" <<'PY'
import json
import sys
from pathlib import Path

trace_dir = Path(sys.argv[1])
preflight_path = trace_dir / "tool_resource_preflight.json"
try:
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    preflight = {
        "stage2_ready": False,
        "preflight_error": f"{type(exc).__name__}: {exc}",
    }
if preflight.get("stage2_ready") is not True:
    (trace_dir / "result_summary.json").write_text(
        json.dumps(
            {
                "task_id": __import__("os").environ.get(
                    "TASK_INSTANCE_ID", ""
                ),
                "agent_exit_code": 3,
                "testbed_exists": Path("/testbed").is_dir(),
                "patch_bytes": 0,
                "has_patch": False,
                "error": "required Stage-2 preflight failed",
                "tool_resource_preflight": preflight,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    raise SystemExit(1)
PY
        then
            echo "[claw] FATAL: required Stage-2 preflight failed"
            exit 3
        fi
        ;;
esac

cd "$CLAW_ROOT/scheduler"

# Install scheduler package (editable, best-effort)
$_CLW_PIP install -e . --quiet 2>/dev/null || $_CLW_PIP install . --quiet 2>/dev/null || true

# Start sidecar.  BCC's system Python package is scoped to this process; do
# not leak it into the agent's task interpreter.
CLAW_SIDECAR_PYTHONPATH="src"
if [ -n "$CLAW_BCC_PYTHONPATH" ]; then
    CLAW_SIDECAR_PYTHONPATH="$CLAW_SIDECAR_PYTHONPATH:$CLAW_BCC_PYTHONPATH"
fi
env "${CLAW_BCC_RUNTIME_ENV[@]}" \
    "PYTHONPATH=$CLAW_SIDECAR_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}" \
    "$_CLW_PYTHON" -m agent_scheduler.main \
    --host 127.0.0.1 --port "$SIDECAR_PORT" \
    > "$TRACE_DIR/sidecar.log" 2>&1 &
SIDECAR_PID=$!
echo "[claw] sidecar PID=$SIDECAR_PID"

# Install a stable launcher path for managed-wrapper exec instrumentation.
# pip may place console scripts under /opt/conda/bin or /usr/local/bin
# depending on the task image, while the plugin config points here.
mkdir -p /opt/claw/bin
cat > /opt/claw/bin/claw-launch <<'EOF_LAUNCHER'
#!/bin/sh
export CLAW_LAUNCHER_PYTHONPATH="/claw/scheduler/src"
export PYTHONPATH="$CLAW_LAUNCHER_PYTHONPATH${PYTHONPATH:+:$PYTHONPATH}"
if [ -x /opt/conda/bin/python3 ]; then
    exec /opt/conda/bin/python3 -m agent_scheduler.launcher "$@"
fi
exec python3 -m agent_scheduler.launcher "$@"
EOF_LAUNCHER
chmod +x /opt/claw/bin/claw-launch
for PIP_NAME in pip pip3; do
    cat > "/opt/claw/bin/$PIP_NAME" <<'EOF_PIP'
#!/bin/sh
if [ -n "${CLAW_TASK_PYTHON:-}" ] && [ -x "$CLAW_TASK_PYTHON" ]; then
    exec "$CLAW_TASK_PYTHON" -m pip "$@"
fi
exec python3 -m pip "$@"
EOF_PIP
    chmod +x "/opt/claw/bin/$PIP_NAME"
done

# Wait for ready
READY=0
for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$SIDECAR_PORT/health/ready" >/dev/null 2>&1; then
        READY=1
        echo "[claw] sidecar ready after ${i}s"
        break
    fi
    sleep 1
done
if [ "$READY" -eq 0 ]; then
    echo "[claw] FATAL: sidecar not ready after 60s"
    {
        echo ""
        echo "=== sidecar readiness failure ==="
        ps -p "$SIDECAR_PID" -o pid,ppid,stat,cmd || true
    } >> "$TRACE_DIR/sidecar.log" 2>&1 || true
    kill "$SIDECAR_PID" 2>/dev/null || true
    exit 1
fi

echo "[claw] === Phase 3: configure OpenClaw ==="
# vLLM provider requires VLLM_API_KEY (any value works).
export VLLM_API_KEY="${LLM_API_KEY:-sk-test}"
export OPENCLAW_MODEL_REF="${OPENCLAW_MODEL_REF:-__MODEL_FULL__}"
export LLM_MODEL="${LLM_MODEL:-__MODEL_SHORT__}"
PROBLEM_STATEMENT_SAFE="${PROBLEM_STATEMENT:-}"
TASK_HINT_TEXT_SAFE="${TASK_HINT_TEXT:-}"

cat > "$TRACE_DIR/task_manifest.json" <<EOF
{
  "task_id": "${TASK_INSTANCE_ID:-}",
  "image": "${TASK_IMAGE:-}",
  "base_commit": "${TASK_BASE_COMMIT:-}",
  "model": "$LLM_MODEL",
  "openclaw_model_ref": "$OPENCLAW_MODEL_REF",
  "problem_statement_bytes": ${#PROBLEM_STATEMENT_SAFE},
  "hint_text_bytes": ${#TASK_HINT_TEXT_SAFE}
}
EOF

# Save ALL Phase 3 diagnostics to a log file for debugging.
# Each command handles its own errors so 'set -e' does not abort.
{
echo "=== openclaw onboard ==="
# --skip-health: we use openclaw agent --local, no gateway needed.
# --accept-risk: required for non-interactive mode.
openclaw onboard --non-interactive --accept-risk --skip-health \
    --mode local --auth-choice vllm \
    --custom-base-url "http://127.0.0.1:$SIDECAR_PORT/v1" \
    --custom-api-key "${LLM_API_KEY:-}" \
    --custom-model-id "$LLM_MODEL" || echo "onboard FAILED (exit=$?)"
echo ""

echo "=== openclaw plugins install ==="
# Copy plugin to writable location to avoid "suspicious ownership" error
# from the read-only /claw bind mount (host uid ≠ container root uid).
cp -r "$CLAW_ROOT/plugin" /tmp/plugin
openclaw plugins install --link /tmp/plugin || echo "plugin install FAILED (exit=$?)"
echo "=== openclaw plugins enable ==="
openclaw plugins enable agent-scheduler || echo "plugin enable FAILED (exit=$?)"
echo ""

if [ -f "$CLAW_ROOT/openclaw-config.json5" ]; then
    echo "=== openclaw config patch ==="
    sed "s/__SANDBOX_CONTAINER_PREFIX__/${AGENT_SCHEDULER_DOCKER_EXEC_CONTAINER_PREFIX:-}/g" \
        "$CLAW_ROOT/openclaw-config.json5" \
        | openclaw config patch --stdin || {
            echo "config patch FAILED (exit=$?)"
            exit 1
        }
    echo ""
fi

echo "=== openclaw models list ==="
openclaw models list || echo "models list FAILED (exit=$?)"
echo ""

echo "=== sidecar /v1/models ==="
curl -sS "http://127.0.0.1:$SIDECAR_PORT/v1/models" || echo "/v1/models FAILED (exit=$?)"
echo ""

echo "=== sidecar /health/ready ==="
curl -sS "http://127.0.0.1:$SIDECAR_PORT/health/ready" || echo "/health/ready FAILED (exit=$?)"
echo ""

echo "=== Phase 3 done ==="
} > "$TRACE_DIR/phase3.log" 2>&1 || true

echo "[claw] === Phase 4: run agent ==="
AGENT_EXIT=0
AGENT_CWD="$CLAW_ROOT/scheduler"
if [ -d /testbed ]; then
    AGENT_CWD="/testbed"
fi
echo "$AGENT_CWD" > "$TRACE_DIR/agent-cwd.txt"

if [ -n "${PROBLEM_STATEMENT:-}" ]; then
    cat > /tmp/problem_statement.txt <<'EOF_PROMPT'
You are running inside a SWE-Rebench task container.

Goal: solve the task by editing the repository inside the container.

Important paths:
- The repository is usually at /testbed. Start there if it exists.
- Trace and smoke-test artifacts are written under /traces.

Workflow:
1. Start by inspecting the repository with shell/file tools.
2. Edit the source files needed for a minimal fix.
3. Run relevant tests or a focused reproduction command.
4. Leave the repository modified with your solution. Do not only explain the fix.
5. If you cannot finish, write down exactly what blocked you.

Do not stop after a prose answer. A useful smoke-test run should leave either
a code diff in /testbed or a clear blocker in your final answer.

Task instance:
EOF_PROMPT
    printf '%s\n\n' "${TASK_INSTANCE_ID:-unknown}" >> /tmp/problem_statement.txt
    printf '%s\n' "Problem statement:" >> /tmp/problem_statement.txt
    printf '%s\n\n' "$PROBLEM_STATEMENT" >> /tmp/problem_statement.txt
    if [ -n "${TASK_HINT_TEXT:-}" ]; then
        printf '%s\n%s\n\n' "Hint:" "$TASK_HINT_TEXT" >> /tmp/problem_statement.txt
    fi
    cp /tmp/problem_statement.txt "$TRACE_DIR/agent_prompt.txt"
    echo "[claw] running in $AGENT_CWD: openclaw agent --local --agent main --model $OPENCLAW_MODEL_REF ..."
    (
        cd "$AGENT_CWD"
        openclaw agent --local \
            --agent main \
            --model "$OPENCLAW_MODEL_REF" \
            --message-file /tmp/problem_statement.txt
    ) > >(tee "$TRACE_DIR/agent-stdout.txt") 2> >(tee "$TRACE_DIR/agent-stderr.txt" >&2) || AGENT_EXIT=$?
else
    echo "[claw] WARNING: PROBLEM_STATEMENT not set"
    (
        cd "$AGENT_CWD"
        bash "$CLAW_ROOT/run_agent.sh"
    ) > >(tee "$TRACE_DIR/agent-stdout.txt") 2> >(tee "$TRACE_DIR/agent-stderr.txt" >&2) || AGENT_EXIT=$?
fi
echo "[claw] agent exited code=$AGENT_EXIT"

echo "[claw] === Phase 5: collect smoke-test artifacts ==="
PATCH_BYTES=0
if [ -d /testbed ]; then
    {
        echo "=== agent cwd ==="
        cat "$TRACE_DIR/agent-cwd.txt" 2>/dev/null || true
        echo ""
        echo "=== collector pwd ==="
        pwd
        echo ""
        echo "=== /testbed git status ==="
        git -C /testbed status --short || true
        echo ""
        echo "=== /testbed git diff --stat ==="
        git -C /testbed diff --stat || true
    } > "$TRACE_DIR/repo_status.txt" 2>&1 || true

    git -C /testbed config --add safe.directory /testbed >/dev/null 2>&1 || true
    if [ -n "${TASK_BASE_COMMIT:-}" ]; then
        git -C /testbed diff "$TASK_BASE_COMMIT" -- . > "$TRACE_DIR/model.patch" 2>/dev/null || true
    else
        git -C /testbed diff -- . > "$TRACE_DIR/model.patch" 2>/dev/null || true
    fi
    if [ -f "$TRACE_DIR/model.patch" ]; then
        PATCH_BYTES=$(wc -c < "$TRACE_DIR/model.patch" | tr -d ' ')
    fi
else
    echo "[claw] WARNING: /testbed not found" > "$TRACE_DIR/repo_status.txt"
fi

cat > "$TRACE_DIR/result_summary.json" <<EOF
{
  "task_id": "${TASK_INSTANCE_ID:-}",
  "agent_exit_code": $AGENT_EXIT,
  "testbed_exists": $([ -d /testbed ] && echo true || echo false),
  "patch_bytes": $PATCH_BYTES,
  "has_patch": $([ "$PATCH_BYTES" -gt 0 ] && echo true || echo false)
}
EOF

echo "[claw] === Phase 6: stop sidecar ==="
kill "$SIDECAR_PID" 2>/dev/null || true
wait "$SIDECAR_PID" 2>/dev/null || true
sleep 2

# Log traces
if [ -f "$TRACE_DIR/trace.jsonl" ]; then
    echo "[claw] trace: $TRACE_DIR/trace.jsonl ($(wc -l < "$TRACE_DIR/trace.jsonl") lines)"
elif compgen -G "$TRACE_DIR/*.jsonl" >/dev/null 2>&1; then
    for f in "$TRACE_DIR"/*.jsonl; do
        echo "[claw] trace: $f ($(wc -l < "$f") lines)"
    done
else
    echo "[claw] WARNING: no trace.jsonl found"
fi
exit $AGENT_EXIT
"""


def _write_entrypoint(bundle_dir: Path, config: RunnerConfig) -> None:
    model_full = config.llm.openclaw_model_ref
    model_short = config.llm.model
    script = (_ENTRYPOINT_TEMPLATE
              .replace("__UPSTREAM__", config.llm.upstream_base_url)
              .replace("__LLM_KEY__", "")
              .replace("__MODEL_FULL__", model_full)
              .replace("__MODEL_SHORT__", model_short)
              .replace("__MAX_TURNS__", str(config.agent.max_turns))
              .replace("__EXTRA__", " ".join(config.agent.extra_args)))
    dest = bundle_dir / "entrypoint.sh"
    dest.write_text(script, encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    _log(f"  Wrote entrypoint.sh ({len(script)} bytes)")


# ══════════════════════════════════════════════════════════════════
#  setup.sh
# ══════════════════════════════════════════════════════════════════

_SETUP_TEMPLATE = r"""#!/bin/bash
# ────────────────────────────────────────────────────────────────
# Environment setup inside swe-rebench containers.
# Installs Node.js, npm, OpenClaw CLI, and Python deps.
# Idempotent -- safe to run multiple times.
#
# Architecture support: x86_64 (amd64), aarch64 (arm64 / Kunpeng).
# BCC/eBPF deps are best-effort and fail-open on all architectures.
# ────────────────────────────────────────────────────────────────
set -euo pipefail

echo "[claw] host arch: $(uname -m)"
""" + _BASH_PYTHON_DETECT + r"""
CLAW_ROOT="${CLAW_ROOT:-/claw}"
SETUP_DONE="/tmp/.claw_setup_done"
SETUP_REVISION="2:mvdan-protocol-3:mvdan-v3.13.1"
MVDAN_STATUS="/tmp/.claw_mvdan_adapter_status.json"

_claw_mvdan_adapter_ready() {
    env "PYTHONPATH=$CLAW_ROOT/scheduler/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$_CLW_PYTHON" -c \
        'from tool_resource.mvdan_client import MvdanClient, default_binary_path; client = MvdanClient(default_binary_path()); client.__enter__(); client.close()' \
        >/dev/null 2>&1
}

if [ -f "$SETUP_DONE" ] \
    && [ "$(cat "$SETUP_DONE" 2>/dev/null || true)" = "$SETUP_REVISION" ] \
    && _claw_mvdan_adapter_ready
then
    echo "[claw] setup already complete, skipping."
    return 0 2>/dev/null || exit 0
fi
if [ -f "$SETUP_DONE" ]; then
    echo "[claw] stale setup marker or mvdan adapter; rerunning setup."
fi

echo "[claw] installing system dependencies..."

# ── Detect package manager ──────────────────────────────────────
if command -v apt-get &>/dev/null; then PKG_MGR="apt"
elif command -v yum &>/dev/null; then PKG_MGR="yum"
elif command -v dnf &>/dev/null; then PKG_MGR="dnf"
elif command -v apk &>/dev/null; then PKG_MGR="apk"
else PKG_MGR="none"
fi

case "$PKG_MGR" in
    apt) apt-get update -qq ;;
    apk) apk update ;;
esac

# ── curl (needed for health checks + nodesource) ────────────────
if ! command -v curl &>/dev/null; then
    echo "[claw] installing curl..."
    case "$PKG_MGR" in
        apt) apt-get install -y -qq curl ;;
        yum) yum install -y -q curl ;;
        dnf) dnf install -y -q curl ;;
        apk) apk add --no-cache curl ;;
    esac
fi

# ── Docker CLI (needed by sidecar DockerExecObserver) ───────────
if ! command -v docker &>/dev/null; then
    echo "[claw] installing docker CLI..."
    case "$PKG_MGR" in
        apt) apt-get install -y -qq docker.io 2>/dev/null || apt-get install -y -qq docker-ce-cli 2>/dev/null || true ;;
        yum) yum install -y -q docker-cli 2>/dev/null || yum install -y -q docker 2>/dev/null || true ;;
        dnf) dnf install -y -q docker-cli 2>/dev/null || dnf install -y -q docker 2>/dev/null || true ;;
        apk) apk add --no-cache docker-cli 2>/dev/null || apk add --no-cache docker 2>/dev/null || true ;;
    esac
fi
if command -v docker &>/dev/null; then
    echo "[claw] docker CLI OK"
else
    echo "[claw] docker CLI not available (DockerExecObserver will idle)"
fi

echo "[claw] installing BCC/eBPF dependencies (best-effort)..."
case "$PKG_MGR" in
    # libbpfcc's Python extension dynamically links libelf.so.1.  Keep this
    # container-only bootstrap fail-open, but install the runtime library
    # explicitly: minimal task images do not always pull it transitively.
    apt) apt-get install -y -qq python3-bpfcc bpfcc-tools libbpfcc libelf1 2>/dev/null || true ;;
    yum) yum install -y -q bcc-tools python3-bcc 2>/dev/null || true ;;
    dnf) dnf install -y -q bcc-tools python3-bcc 2>/dev/null || true ;;
    apk) apk add --no-cache bcc-tools bcc-python3 2>/dev/null || true ;;
esac
case "$PKG_MGR" in
    apt)
        apt-get install -y -qq clang llvm kmod linux-headers-"$(uname -r)" 2>/dev/null \
            || apt-get install -y -qq clang llvm kmod linux-headers-generic 2>/dev/null \
            || apt-get install -y -qq clang llvm kmod 2>/dev/null \
            || true
        ;;
    yum) yum install -y -q clang llvm kmod kernel-headers kernel-devel 2>/dev/null || yum install -y -q clang llvm kmod 2>/dev/null || true ;;
    dnf) dnf install -y -q clang llvm kmod kernel-headers kernel-devel 2>/dev/null || dnf install -y -q clang llvm kmod 2>/dev/null || true ;;
    apk) apk add --no-cache clang llvm kmod linux-headers 2>/dev/null || true ;;
esac
if [ -d /usr/lib/python3/dist-packages/bcc ]; then
    echo "/usr/lib/python3/dist-packages" > /tmp/.claw_bcc_pythonpath
else
    _CLAW_BCC_PATH="$(find /usr/lib /usr/lib64 -path '*/site-packages/bcc' -type d -print -quit 2>/dev/null || true)"
    if [ -n "$_CLAW_BCC_PATH" ]; then
        dirname "$_CLAW_BCC_PATH" > /tmp/.claw_bcc_pythonpath
    fi
fi

# Some minimized benchmark images retain dpkg's "installed" record for
# libelf1 after removing its shared-object payload.  Conda-based images can
# also force an older libstdc++.so.6 into the selected Python even though BCC
# and libclang were installed from the system package manager.  Repair only
# those observed container failures; all other optional BCC failures remain
# fail-open.
_CLAW_BCC_PYTHONPATH=""
_CLAW_BCC_LD_PRELOAD=""
_CLAW_BCC_PRELOAD_FILE="/tmp/.claw_bcc_ld_preload"
# SETUP_DONE returns before this point, so a completed setup keeps its verified
# marker while a fresh or resumed setup cannot inherit a stale one.
rm -f -- "$_CLAW_BCC_PRELOAD_FILE" || true

_claw_import_bcc() {
    if [ -n "$_CLAW_BCC_LD_PRELOAD" ]; then
        LD_PRELOAD="${_CLAW_BCC_LD_PRELOAD}${LD_PRELOAD:+:$LD_PRELOAD}" \
        PYTHONPATH="${_CLAW_BCC_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}" \
            "$_CLW_PYTHON" -c "import bcc"
    else
        PYTHONPATH="${_CLAW_BCC_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}" \
            "$_CLW_PYTHON" -c "import bcc"
    fi
}

_claw_try_system_libstdcxx() {
    local candidate
    local candidate_error=""

    if ! command -v ldconfig &>/dev/null; then
        return 1
    fi
    while IFS= read -r candidate; do
        [ -r "$candidate" ] || continue
        case "$candidate" in
            /lib/*|/lib64/*|/usr/lib/*|/usr/lib64/*) ;;
            *) continue ;;
        esac
        _CLAW_BCC_LD_PRELOAD="$candidate"
        if candidate_error="$(_claw_import_bcc 2>&1)"; then
            if printf '%s\n' "$candidate" > "$_CLAW_BCC_PRELOAD_FILE" \
                && chmod 0600 "$_CLAW_BCC_PRELOAD_FILE"
            then
                echo "[claw] BCC runtime repaired with system libstdc++ (sidecar-only): $candidate"
                return 0
            fi
            candidate_error="verified $candidate but could not persist the sidecar-only preload marker"
            rm -f "$_CLAW_BCC_PRELOAD_FILE" || true
        fi
        _CLAW_BCC_LD_PRELOAD=""
        _CLAW_BCC_PRELOAD_ERROR="$candidate_error"
    done < <(
        ldconfig -p 2>/dev/null \
            | awk '$1 == "libstdc++.so.6" && !seen[$NF]++ { print $NF }'
    )
    return 1
}

if [ -s /tmp/.claw_bcc_pythonpath ]; then
    _CLAW_BCC_PYTHONPATH="$(cat /tmp/.claw_bcc_pythonpath)"
    _CLAW_BCC_IMPORT_ERROR=""
    if ! _CLAW_BCC_IMPORT_ERROR="$(_claw_import_bcc 2>&1)"; then
        if [ "$PKG_MGR" = "apt" ]; then
            case "$_CLAW_BCC_IMPORT_ERROR" in
                *"libelf.so.1"*)
                    echo "[claw] libelf.so.1 is missing; reinstalling libelf1..."
                    if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --reinstall libelf1; then
                        if command -v ldconfig &>/dev/null; then
                            ldconfig 2>/dev/null || true
                        fi
                        if _CLAW_BCC_RECHECK_ERROR="$(_claw_import_bcc 2>&1)"; then
                            _CLAW_BCC_IMPORT_ERROR=""
                            echo "[claw] libelf1 reinstall repaired the BCC runtime"
                        else
                            _CLAW_BCC_IMPORT_ERROR="$_CLAW_BCC_RECHECK_ERROR"
                        fi
                    else
                        echo "[claw] libelf1 reinstall failed (Stage-2 will remain unavailable)"
                    fi
                    ;;
            esac
        fi
        if [ -n "$_CLAW_BCC_IMPORT_ERROR" ]; then
            case "$_CLAW_BCC_IMPORT_ERROR" in
                *"libstdc++.so.6"*GLIBCXX_*"not found"*)
                    echo "[claw] Conda libstdc++ is incompatible with system BCC; probing a sidecar-only system preload..."
                    if _claw_try_system_libstdcxx; then
                        _CLAW_BCC_IMPORT_ERROR=""
                    else
                        _CLAW_BCC_IMPORT_ERROR="${_CLAW_BCC_PRELOAD_ERROR:-$_CLAW_BCC_IMPORT_ERROR}"
                    fi
                    ;;
            esac
        fi
        if [ -n "$_CLAW_BCC_IMPORT_ERROR" ]; then
            echo "[claw] BCC remains unavailable after container repair probes: $_CLAW_BCC_IMPORT_ERROR"
        fi
    fi
fi

# ── Python 3 (system fallback -- usually conda is already present) ──
if ! $_CLW_PYTHON --version &>/dev/null 2>&1; then
    echo "[claw] installing python3..."
    case "$PKG_MGR" in
        apt) apt-get install -y -qq python3 python3-pip ;;
        yum) yum install -y -q python3 python3-pip ;;
        dnf) dnf install -y -q python3 python3-pip ;;
        apk) apk add --no-cache python3 py3-pip ;;
        *)  echo "[claw] FATAL: cannot install python3" ; exit 1 ;;
    esac
    _CLW_PYTHON="$(command -v python3)"
    _CLW_PIP="$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null)"
fi
echo "[claw] python=$_CLW_PYTHON"

# ── Node.js 24 (direct tarball, no gpg needed) ──────────────────
NODE_OK=0
if command -v node &>/dev/null && node --version &>/dev/null 2>&1; then
    NODE_OK=1
fi
if [ "$NODE_OK" -eq 0 ]; then
    echo "[claw] installing Node.js (direct download)..."
    NODE_ARCH="x64"
    case "$(uname -m)" in
        aarch64|arm64) NODE_ARCH="arm64" ;;
    esac
    # Resolve the current Node.js 24 archive instead of pinning a patch release
    # that disappears from the latest-v24.x alias. Select the detected
    # architecture from the checksum manifest so ARM does not fall back to x64.
    NODE_BASE_URL="https://nodejs.org/dist/latest-v24.x"
    if ! NODE_SHASUMS="$(curl -fsSL "$NODE_BASE_URL/SHASUMS256.txt")"; then
        echo "[claw] FATAL: cannot resolve the latest Node.js 24 release"
        exit 1
    fi
    LATEST="$(
        printf '%s\n' "$NODE_SHASUMS" \
            | awk -v arch="$NODE_ARCH" '$2 ~ ("-linux-" arch "\\.tar\\.xz$") { print $2; exit }'
    )"
    if [ -z "$LATEST" ]; then
        echo "[claw] FATAL: no Node.js 24 archive for architecture $NODE_ARCH"
        exit 1
    fi
    curl -fsSL "$NODE_BASE_URL/$LATEST" -o "/tmp/node.tar.xz"
    tar -xJf "/tmp/node.tar.xz" -C /usr/local --strip-components=1
    rm -f "/tmp/node.tar.xz"
fi

# Verify node actually works
if node --version &>/dev/null 2>&1; then
    echo "[claw] node $(node --version) OK"
else
    echo "[claw] FATAL: node installed but does not run"
    ldd "$(command -v node)" 2>&1 | grep "not found" || true
    exit 1
fi

# ── OpenClaw CLI ─────────────────────────────────────────────────
if ! command -v openclaw &>/dev/null; then
    echo "[claw] installing openclaw CLI..."
    npm install -g openclaw@2026.7.1 2>/dev/null || npm install -g openclaw 2>/dev/null || {
        echo "[claw] FATAL: openclaw install failed"
        exit 1
    }
fi
echo "[claw] openclaw $(openclaw --version 2>&1 | head -1)"

# ── Sidecar Python deps ─────────────────────────────────────────
echo "[claw] installing sidecar Python deps..."
$_CLW_PIP install --quiet \
    fastapi uvicorn pydantic psutil httpx prometheus-client numpy \
    2>&1 | tail -1
if [ -s /tmp/.claw_bcc_pythonpath ]; then
    export PYTHONPATH="$(cat /tmp/.claw_bcc_pythonpath)${PYTHONPATH:+:$PYTHONPATH}"
fi
_CLAW_BCC_RUNTIME_ENV=()
if [ -s "$_CLAW_BCC_PRELOAD_FILE" ]; then
    IFS= read -r _CLAW_BCC_LD_PRELOAD < "$_CLAW_BCC_PRELOAD_FILE" \
        || _CLAW_BCC_LD_PRELOAD=""
    case "$_CLAW_BCC_LD_PRELOAD" in
        /lib/*|/lib64/*|/usr/lib/*|/usr/lib64/*)
            if [ -r "$_CLAW_BCC_LD_PRELOAD" ]; then
                _CLAW_BCC_RUNTIME_ENV=(
                    "LD_PRELOAD=${_CLAW_BCC_LD_PRELOAD}${LD_PRELOAD:+:$LD_PRELOAD}"
                )
            else
                _CLAW_BCC_LD_PRELOAD=""
            fi
            ;;
        *) _CLAW_BCC_LD_PRELOAD="" ;;
    esac
fi
if [ -n "$_CLAW_BCC_LD_PRELOAD" ]; then
    if ! env "${_CLAW_BCC_RUNTIME_ENV[@]}" "$_CLW_PYTHON" \
        -c "import fastapi, uvicorn, pydantic, psutil, numpy, bcc; print('[claw] sidecar deps and BCC OK with system libstdc++')"
    then
        echo "[claw] system libstdc++ preload failed the combined sidecar/BCC probe; disabling the Stage-2 preload"
        rm -f "$_CLAW_BCC_PRELOAD_FILE" || true
        _CLAW_BCC_LD_PRELOAD=""
        _CLAW_BCC_RUNTIME_ENV=()
        "$_CLW_PYTHON" -c "import fastapi, uvicorn, pydantic, psutil, numpy; print('[claw] sidecar deps OK')"
    fi
else
    "$_CLW_PYTHON" -c "import fastapi, uvicorn, pydantic, psutil, numpy; print('[claw] sidecar deps OK')"
fi
env "${_CLAW_BCC_RUNTIME_ENV[@]}" "$_CLW_PYTHON" - <<'PY' || true
try:
    import bcc  # noqa: F401
    print("[claw] BCC Python binding OK")
except Exception as exc:
    print(f"[claw] BCC Python binding unavailable: {type(exc).__name__}: {exc}")
PY

# ── Done ────────────────────────────────────────────────────────
echo "[claw] building/verifying pinned mvdan adapter..."
if env "PYTHONPATH=$CLAW_ROOT/scheduler/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$_CLW_PYTHON" - "$MVDAN_STATUS" <<'PY'
import json
import os
import sys
from pathlib import Path

from tool_resource.mvdan_client import (
    ADAPTER_PROTOCOL_VERSION,
    PARSER_NAME,
    PARSER_VERSION,
    REQUIRED_CAPABILITIES,
    MvdanClient,
    default_binary_path,
    ensure_compatible_adapter,
)

status_path = Path(sys.argv[1])
binary_path = default_binary_path()
status = {
    "attempted": True,
    "ok": False,
    "binary_path": str(binary_path),
    "cache_present_before": binary_path.is_file(),
    "parser": {"name": PARSER_NAME, "version": PARSER_VERSION},
    "protocol": {
        "version": ADAPTER_PROTOCOL_VERSION,
        "required_capabilities": sorted(REQUIRED_CAPABILITIES),
    },
    "error": None,
}
try:
    built_path = ensure_compatible_adapter()
    with MvdanClient(built_path):
        pass
    status.update(
        {
            "ok": True,
            "binary_path": str(built_path),
            "binary_exists": built_path.is_file(),
            "binary_executable": os.access(built_path, os.X_OK),
        }
    )
except Exception as exc:
    status["error"] = f"{type(exc).__name__}: {exc}"

status_path.parent.mkdir(parents=True, exist_ok=True)
temporary_path = status_path.with_name(
    f"{status_path.name}.{os.getpid()}.tmp"
)
temporary_path.write_text(
    json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
os.chmod(temporary_path, 0o600)
os.replace(temporary_path, status_path)
if not status["ok"]:
    raise SystemExit(1)
print(
    "[claw] mvdan adapter OK "
    f"({PARSER_NAME} {PARSER_VERSION}, protocol {ADAPTER_PROTOCOL_VERSION})"
)
PY
then
    printf '%s\n' "$SETUP_REVISION" > "$SETUP_DONE.$$"
    chmod 0600 "$SETUP_DONE.$$"
    mv -f "$SETUP_DONE.$$" "$SETUP_DONE"
else
    rm -f -- "$SETUP_DONE"
    echo "[claw] mvdan adapter unavailable (Stage-2 will remain unavailable)"
fi

echo "[claw] setup complete."
"""


def _write_setup_script(bundle_dir: Path) -> None:
    dest = bundle_dir / "setup.sh"
    dest.write_text(_SETUP_TEMPLATE, encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    _log(f"  Wrote setup.sh ({len(_SETUP_TEMPLATE)} bytes)")


# ══════════════════════════════════════════════════════════════════
#  run_agent.sh (fallback)
# ══════════════════════════════════════════════════════════════════

_RUN_AGENT_TEMPLATE = r"""#!/bin/bash
set -euo pipefail
echo "[claw] running agent (fallback)..."
echo "[claw] TASK_INSTANCE_ID=${TASK_INSTANCE_ID:-unknown}"
exec openclaw agent --local \
    --agent main \
    --model "__MODEL_FULL__" \
    --message "${PROBLEM_STATEMENT:-Solve the task.}"
"""


def _write_run_agent(bundle_dir: Path, config: RunnerConfig) -> None:
    model_full = config.llm.openclaw_model_ref
    script = (_RUN_AGENT_TEMPLATE
              .replace("__MODEL_FULL__", model_full)
              .replace("__MAX_TURNS__", str(config.agent.max_turns))
              .replace("__EXTRA__", " ".join(config.agent.extra_args)))
    dest = bundle_dir / "run_agent.sh"
    dest.write_text(script, encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    _log(f"  Wrote run_agent.sh ({len(script)} bytes)")


# ── helpers ──────────────────────────────────────────────────────

def _write_plugin_config(bundle_dir: Path) -> None:
    cfg = json.dumps({
        "agents": {
            "defaults": {
                "workspace": "/testbed",
                "repoRoot": "/testbed",
                "sandbox": {
                    "docker": {
                        "containerPrefix": "__SANDBOX_CONTAINER_PREFIX__",
                    },
                },
            },
        },
        "tools": {
            "exec": {
                "pathPrepend": [
                    "/opt/claw/bin",
                    "/opt/miniconda3/envs/testbed/bin",
                    "/opt/conda/envs/testbed/bin",
                    "/opt/miniconda3/condabin",
                    "/opt/miniconda3/bin",
                    "/opt/conda/bin",
                ],
            },
        },
        "env": {
            "CLAW_SCHEDULER_ENDPOINT": "http://127.0.0.1:8765",
            "OPENCLAW_SCHEDULER_ENDPOINT": "http://127.0.0.1:8765",
            "CLAW_EXEC_WORKDIR": "/testbed",
            "OPENCLAW_WORKSPACE_DIR": "/testbed",
            "OPENCLAW_REPO_ROOT": "/testbed",
        },
        "plugins": {"entries": {"agent-scheduler": {"enabled": True, "config": _PLUGIN_CONFIG}}}
    }, indent=2) + "\n"
    dest = bundle_dir / "openclaw-config.json5"
    dest.write_text(cfg, encoding="utf-8")
    _log("  Wrote openclaw-config.json5")


def _copytree_selective(src: Path, dst: Path, skip: set[str]) -> None:
    if not src.exists():
        _log(f"  [warn] source not found: {src}")
        return
    import fnmatch
    def _ignore(_dir: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name in skip:
                ignored.add(name)
                continue
            for pat in skip:
                if "*" in pat and fnmatch.fnmatch(name, pat):
                    ignored.add(name)
                    break
        return ignored
    shutil.copytree(str(src), str(dst), ignore=_ignore, dirs_exist_ok=True)


def _remove_tree(path: Path) -> None:
    def _make_writable_and_retry(func: object, item: str, _exc_info: object) -> None:
        try:
            os.chmod(item, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass
        try:
            func(item)  # type: ignore[operator]
        except OSError:
            raise

    shutil.rmtree(path, onerror=_make_writable_and_retry)


def _count_files(directory: Path) -> int:
    return sum(1 for _ in directory.rglob("*") if _.is_file())


def _tail_text(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"cannot read log: {exc}"
    return text[-max_chars:]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── CLI ──────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build the swe-rebench runtime bundle.")
    parser.add_argument("--config", default="swe_rebench/config.example.yaml")
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args()
    repo_root = Path(args.repo_root) if args.repo_root else _detect_repo_root()
    cfg = RunnerConfig.from_yaml(args.config, repo_root=repo_root)
    bundle_path = build_bundle(cfg)
    print(f"Bundle ready: {bundle_path}")


def _detect_repo_root() -> Path:
    p = Path(__file__).resolve()
    for _ in range(6):
        if (p / "AGENTS.md").exists():
            return p
        p = p.parent
    return Path.cwd()


if __name__ == "__main__":
    main()
