#!/bin/bash
# Verify the telemetry.py fix v2
cd ~/claw
source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null || true
conda activate ML 2>/dev/null || true

echo "=== Syntax check ==="
python3 -c "
import py_compile
py_compile.compile('/home/weitian/claw/services/scheduler/src/tool_resource/telemetry.py', doraise=True)
print('SYNTAX OK')
"

echo "=== Import check ==="
PYTHONPATH=/home/weitian/claw/services/scheduler/src python3 -c "
from tool_resource.telemetry import _container_pid_set
import inspect
sig = inspect.signature(_container_pid_set)
print('cgroup_inodes param:', 'cgroup_inodes' in sig.parameters)
print('IMPORT OK')
"

echo "=== Test 1: cgroup-no-cross-leak ==="
PYTHONPATH=/home/weitian/claw/services/scheduler/src python3 -c "
from tool_resource.telemetry import _container_pid_set
events = [
    {'host_pid': 100, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 1},
    {'host_pid': 200, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 100},
    {'host_pid': 300, 'cgroup_id': 99, 'type': 'exec_boundary', 'parent_host_pid': 200},
]
result = _container_pid_set(events, 100, cgroup_inodes={42})
print('Result:', sorted(result))
assert 100 in result and 200 in result, 'Cgroup PIDs not discovered'
assert 300 not in result, 'Foreign cgroup PID leaked'
print('PASS')
"

echo "=== Test 2: Docker exec scenario ==="
PYTHONPATH=/home/weitian/claw/services/scheduler/src python3 -c "
from tool_resource.telemetry import _container_pid_set
events = [
    {'host_pid': 1000, 'cgroup_id': 1, 'type': 'fork', 'child_host_pid': 2000, 'parent_host_pid': 0},
    {'host_pid': 2000, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 1000},
    {'host_pid': 2000, 'cgroup_id': 42, 'type': 'fork', 'child_host_pid': 2001, 'parent_host_pid': 2000},
    {'host_pid': 2001, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 2000},
]
result = _container_pid_set(events, 1, cgroup_inodes={42})
print('Result:', sorted(result))
assert 2000 in result, 'Docker exec PID not discovered'
assert 2001 in result, 'Docker exec child not discovered'
assert 1000 not in result, 'Containerd PID leaked'
print('PASS')
"

echo "=== Test 3: Legacy lineage fallback ==="
PYTHONPATH=/home/weitian/claw/services/scheduler/src python3 -c "
from tool_resource.telemetry import _container_pid_set
events = [
    {'host_pid': 100, 'type': 'fork', 'child_host_pid': 200, 'parent_host_pid': 0},
    {'host_pid': 200, 'type': 'exec_boundary', 'parent_host_pid': 100},
]
result = _container_pid_set(events, 100)
print('Result:', sorted(result))
assert 100 in result and 200 in result, 'Legacy lineage failed'
result2 = _container_pid_set(events, 100, cgroup_inodes=set())
print('Empty cgroup_inodes result:', sorted(result2))
assert 200 in result2, 'Empty cgroup_inodes fallback failed'
print('PASS')
"

echo "=== ALL TESTS PASSED ==="
