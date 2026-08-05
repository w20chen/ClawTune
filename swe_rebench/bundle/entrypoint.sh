#!/bin/bash
set -euo pipefail
CLAW_ROOT="/claw"
TRACE_DIR="/traces"
SIDECAR_PORT=8765

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
export AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL="${LLM_UPSTREAM_BASE_URL:-https://api.deepseek.com}"
export AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY="${LLM_API_KEY:-}"
export AGENT_SCHEDULER_LLM_PROXY_ENABLED="true"
# Model spoofing: the sidecar auto-normalises upstream /v1/models by default.
# Setting both vars explicitly provides a synthetic fallback for cases where
# the upstream /models endpoint is unreachable or returns unparseable data.
# Set UPSTREAM_MODEL to a different value to translate model names.
export AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL="${LLM_MODEL:-deepseek-v4-flash}"
export AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL="${LLM_MODEL:-deepseek-v4-flash}"
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
export OPENCLAW_MODEL_REF="${OPENCLAW_MODEL_REF:-vllm/deepseek-v4-flash}"
export LLM_MODEL="${LLM_MODEL:-deepseek-v4-flash}"
PROBLEM_STATEMENT_SAFE="${PROBLEM_STATEMENT:-}"
TASK_HINT_TEXT_SAFE="${TASK_HINT_TEXT:-}"

cat > "$TRACE_DIR/task_manifest.json" <<EOF
{
  "task_id": "${TASK_INSTANCE_ID:-}",
  "repo": "${AGENT_SCHEDULER_TOOL_RESOURCE_REPO:-}",
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
    echo "[claw] running in $AGENT_CWD: openclaw agent (--local, --model $OPENCLAW_MODEL_REF) ..."
    (
        cd "$AGENT_CWD"
        # OpenClaw 2026.7.x uses `--agent main`; newer builds moved the agent
        # id to a positional subcommand (`openclaw agent main ...`).  Match the
        # installed binary so the CLI does not reject the invocation.  A flag
        # build answers `agent main --help` with the parent usage or the "Too
        # many arguments ... Try: openclaw agent main --help" hint (which also
        # contains "agent main"), so only the real subcommand usage line on
        # stdout proves a positional build.
        if openclaw agent main --help 2>/dev/null | grep -q 'Usage: openclaw agent main'; then
            openclaw agent main --local \
                --model "$OPENCLAW_MODEL_REF" \
                --message-file /tmp/problem_statement.txt
        else
            openclaw agent --local \
                --agent main \
                --model "$OPENCLAW_MODEL_REF" \
                --message-file /tmp/problem_statement.txt
        fi
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
