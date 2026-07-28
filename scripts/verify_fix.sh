#!/bin/bash
# Verify the telemetry.py fix
cd ~/claw
source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null || true
conda activate ML 2>/dev/null || true

echo "=== Python version ==="
python3 --version

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
print('Function signature:', sig)
print('Has cgroup_inodes param:', 'cgroup_inodes' in sig.parameters)
print('IMPORT OK')
"

echo "=== Cgroup PID set test ==="
PYTHONPATH=/home/weitian/claw/services/scheduler/src python3 -c "
from tool_resource.telemetry import _container_pid_set

# Test 1: cgroup-based discovery works
events = [
    {'host_pid': 100, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 1},
    {'host_pid': 200, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 100},
    {'host_pid': 300, 'cgroup_id': 99, 'type': 'exec_boundary', 'parent_host_pid': 200},
]
result = _container_pid_set(events, 100, cgroup_inodes={42})
print('Test 1 - cgroup discovery:', sorted(result))
assert 100 in result and 200 in result, 'Cgroup-based PID discovery failed'
assert 300 not in result, 'Foreign cgroup PID leaked in'

# Test 2: without cgroup_inodes, old behavior preserved
result2 = _container_pid_set(events, 100)
print('Test 2 - lineage only:', sorted(result2))

# Test 3: Docker exec scenario (parent outside container)
events3 = [
    {'host_pid': 1000, 'cgroup_id': 1, 'type': 'fork', 'child_host_pid': 2000, 'parent_host_pid': 0},
    {'host_pid': 2000, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 1000},
    {'host_pid': 2000, 'cgroup_id': 42, 'type': 'fork', 'child_host_pid': 2001, 'parent_host_pid': 2000},
    {'host_pid': 2001, 'cgroup_id': 42, 'type': 'exec_boundary', 'parent_host_pid': 2000},
]
result3 = _container_pid_set(events3, 1, cgroup_inodes={42})
print('Test 3 - Docker exec:', sorted(result3))
assert 2000 in result3, 'Docker exec PID not discovered via cgroup'
assert 2001 in result3, 'Docker exec child PID not discovered'
print('ALL TESTS PASSED')
"

echo "=== DONE ==="
