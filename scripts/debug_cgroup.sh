#!/bin/bash
# Live debug: check cgroup and BPF event flow
set -e
cd ~/claw
source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null || true
conda activate ML 2>/dev/null || true

echo "=== 1. Find a running Docker container ==="
CID=$(docker ps -q --filter "name=claw-srb" | head -1)
if [ -z "$CID" ]; then
    echo "No claw-srb container running. Starting a test..."
    # Start a simple test container
    CID=$(docker run -d --rm --name claw-srb-debug alpine sleep 300)
    echo "Started test container: $CID"
fi
echo "Container ID: $CID"

echo ""
echo "=== 2. Inspect cgroup ==="
CGROUP_PATH=$(docker inspect "$CID" --format '{{.HostConfig.CgroupParent}}' 2>/dev/null || echo "")
if [ -z "$CGROUP_PATH" ]; then
    # Get the cgroup path from /proc
    INIT_PID=$(docker inspect "$CID" --format '{{.State.Pid}}')
    CGROUP_PATH=$(cat /proc/$INIT_PID/cgroup 2>/dev/null | grep '0::' | cut -d: -f3 || echo "")
    if [ -n "$CGROUP_PATH" ]; then
        CGROUP_PATH="/sys/fs/cgroup$CGROUP_PATH"
    fi
fi
echo "Init PID: $INIT_PID"
echo "Cgroup path: $CGROUP_PATH"
if [ -d "$CGROUP_PATH" ]; then
    CGROUP_INODE=$(stat -c %i "$CGROUP_PATH" 2>/dev/null || echo "N/A")
    echo "Cgroup inode: $CGROUP_INODE"
    echo "Cgroup procs count: $(cat $CGROUP_PATH/cgroup.procs 2>/dev/null | wc -l)"
    echo "Cgroup procs: $(cat $CGROUP_PATH/cgroup.procs 2>/dev/null | tr '\n' ' ')"
else
    echo "Cgroup path does not exist!"
fi

echo ""
echo "=== 3. Check bpf_get_current_cgroup_id for container process ==="
if [ -n "$INIT_PID" ] && [ "$INIT_PID" -gt 0 ]; then
    # Read cgroup ID from /proc
    CGROUP_INODE2=$(stat -c %i /proc/$INIT_PID/ns/cgroup 2>/dev/null || echo "N/A")
    # Actually, the inode is from the cgroup fs, not the namespace
    # Let's check what cgroup the init process is in
    cat /proc/$INIT_PID/cgroup 2>/dev/null | head -5
fi

echo ""
echo "=== 4. Test _container_pid_set with cgroup_inodes in isolation ==="
PYTHONPATH=/home/weitian/claw/services/scheduler/src python3 -c "
from tool_resource.telemetry import _container_pid_set

# Simulate the scenario
cgroup_inodes = {$CGROUP_INODE} if '$CGROUP_INODE' != 'N/A' else set()
print('cgroup_inodes:', cgroup_inodes)

# Test with synthetic events matching the real scenario
events = [
    # Docker exec creates a process
    {'host_pid': 99999, 'cgroup_id': $CGROUP_INODE, 'type': 'exec_boundary', 'parent_host_pid': 1},
    # Shell fork
    {'host_pid': 99999, 'cgroup_id': $CGROUP_INODE, 'type': 'fork', 'child_host_pid': 99998, 'parent_host_pid': 1},
    # Command exec
    {'host_pid': 99998, 'cgroup_id': $CGROUP_INODE, 'type': 'exec_boundary', 'parent_host_pid': 99999},
]
result = _container_pid_set(events, $INIT_PID, cgroup_inodes=cgroup_inodes)
print('Result PIDs:', sorted(result))
print('Expected: all 3 PIDs should be in the set')
"

echo ""
echo "=== 5. Check what cgroup_inodes are discovered ==="
PYTHONPATH=/home/weitian/claw/services/scheduler/src python3 -c "
from tool_resource.telemetry import _discover_leaf_cgroup_inodes
from pathlib import Path
if '$CGROUP_PATH' and Path('$CGROUP_PATH').is_dir():
    inodes = _discover_leaf_cgroup_inodes(Path('$CGROUP_PATH'))
    print('Discovered inodes:', sorted(inodes))
    print('Count:', len(inodes))
else:
    print('Cgroup path not available')
"

echo ""
echo "=== 6. Run a quick exec in the container and check BPF ==="
if [ -n "$CID" ]; then
    echo "Running: docker exec $CID /bin/true"
    docker exec "$CID" /bin/true 2>&1
    echo "Exit: $?"
fi

echo ""
echo "=== DONE ==="
# Cleanup test container
if [ "$CID" = "$(docker ps -q --filter name=claw-srb-debug)" ]; then
    docker rm -f "$CID" 2>/dev/null || true
fi
