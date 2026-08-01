#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi
export PYTHONPATH="${REPO_ROOT}/services/scheduler/src${PYTHONPATH:+:${PYTHONPATH}}"
export CLAWTUNE_TELEMETRY_SOURCE="${REPO_ROOT}/services/scheduler/src/tool_resource/telemetry.py"

echo "=== Python version ==="
"${PYTHON}" --version

echo "=== Syntax and import checks ==="
"${PYTHON}" - <<'PY'
import inspect
import os
import py_compile

py_compile.compile(os.environ["CLAWTUNE_TELEMETRY_SOURCE"], doraise=True)
from tool_resource.telemetry import _container_pid_set

signature = inspect.signature(_container_pid_set)
assert "cgroup_inodes" in signature.parameters
print("Syntax and import checks passed")
PY

echo "=== Cgroup attribution checks ==="
"${PYTHON}" - <<'PY'
from tool_resource.telemetry import _container_pid_set

events = [
    {"host_pid": 100, "cgroup_id": 42, "type": "exec_boundary", "parent_host_pid": 1},
    {"host_pid": 200, "cgroup_id": 42, "type": "exec_boundary", "parent_host_pid": 100},
    {"host_pid": 300, "cgroup_id": 99, "type": "exec_boundary", "parent_host_pid": 200},
]
result = _container_pid_set(events, 100, cgroup_inodes={42})
assert 100 in result and 200 in result
assert 300 not in result

docker_exec_events = [
    {"host_pid": 1000, "cgroup_id": 1, "type": "fork", "child_host_pid": 2000, "parent_host_pid": 0},
    {"host_pid": 2000, "cgroup_id": 42, "type": "exec_boundary", "parent_host_pid": 1000},
    {"host_pid": 2000, "cgroup_id": 42, "type": "fork", "child_host_pid": 2001, "parent_host_pid": 2000},
    {"host_pid": 2001, "cgroup_id": 42, "type": "exec_boundary", "parent_host_pid": 2000},
]
result = _container_pid_set(docker_exec_events, 1, cgroup_inodes={42})
assert 2000 in result and 2001 in result
assert 1000 not in result

lineage_events = [
    {"host_pid": 100, "type": "fork", "child_host_pid": 200, "parent_host_pid": 0},
    {"host_pid": 200, "type": "exec_boundary", "parent_host_pid": 100},
]
result = _container_pid_set(lineage_events, 100, cgroup_inodes=set())
assert 100 in result and 200 in result
print("All cgroup attribution checks passed")
PY
