#!/usr/bin/env bash
# eBPF clause telemetry diagnostic script
# Run on the server: bash diag_ebpf.sh
set -euo pipefail

echo "=============================================="
echo "eBPF Clause Telemetry Diagnostic"
echo "=============================================="
echo ""

# 1. Check kernel and BPF support
echo "--- [1] Kernel & BPF ---"
uname -r
echo "CONFIG_BPF_KPROBE_OVERRIDE:"
grep CONFIG_BPF_KPROBE_OVERRIDE /boot/config-$(uname -r) 2>/dev/null || echo "  (not found)"
echo "CONFIG_DEBUG_INFO_BTF:"
grep CONFIG_DEBUG_INFO_BTF /boot/config-$(uname -r) 2>/dev/null || echo "  (not found)"
echo ""

# 2. Check if kprobes are working at all
echo "--- [2] Kprobe events ---"
echo "Current kprobe events count:"
sudo cat /sys/kernel/debug/tracing/kprobe_events 2>/dev/null | wc -l || echo "  (cannot read)"
echo ""

# 3. Test bpftrace to see if it captures any exec
echo "--- [3] bpftrace exec test (5 seconds) ---"
if command -v bpftrace &>/dev/null; then
    echo "bpftrace available. Capturing exec events for 5 seconds..."
    sudo timeout 5 bpftrace -e 'tracepoint:syscalls:sys_enter_execve { printf("exec: %s pid=%d cgroup=%llu\n", str(args->filename), pid, cgroupid); }' 2>&1 || true
else
    echo "bpftrace not installed. Install with: sudo apt install bpftrace"
fi
echo ""

# 4. Check running Docker containers and their cgroups
echo "--- [4] Docker containers ---"
sudo docker ps --format "{{.ID}} {{.Names}} {{.Image}}" 2>/dev/null || echo "  (no docker or not running)"
echo ""

# If there's a running container, inspect its cgroup
CONTAINER_ID=$(sudo docker ps -q 2>/dev/null | head -1)
if [ -n "$CONTAINER_ID" ]; then
    echo "--- [5] Container cgroup detail ---"
    INIT_PID=$(sudo docker inspect --format '{{.State.Pid}}' "$CONTAINER_ID" 2>/dev/null)
    echo "Container ID: $CONTAINER_ID"
    echo "Init PID (host): $INIT_PID"
    echo ""
    echo "Container /proc/$INIT_PID/cgroup:"
    sudo cat /proc/$INIT_PID/cgroup 2>/dev/null || echo "  (cannot read)"
    echo ""
    
    # Find the cgroup path
    CGROUP_PATH=""
    if [ -n "$INIT_PID" ]; then
        CGROUP_LINE=$(sudo cat /proc/$INIT_PID/cgroup 2>/dev/null | grep "^0::" | head -1)
        if [ -n "$CGROUP_LINE" ]; then
            CGROUP_REL=$(echo "$CGROUP_LINE" | cut -d: -f3)
            CGROUP_PATH="/sys/fs/cgroup$CGROUP_REL"
            echo "Cgroup path: $CGROUP_PATH"
            echo "Cgroup inode: $(stat -c '%i' "$CGROUP_PATH" 2>/dev/null || echo 'unknown')"
            echo ""
            
            echo "Child cgroups under $CGROUP_PATH:"
            sudo find "$CGROUP_PATH" -type d 2>/dev/null | while read d; do
                inode=$(stat -c '%i' "$d" 2>/dev/null || echo '?')
                procs=$(sudo cat "$d/cgroup.procs" 2>/dev/null | wc -l || echo '0')
                echo "  inode=$inode procs=$procs $d"
            done
            echo ""
            
            echo "Processes in cgroup (first 10):"
            sudo cat "$CGROUP_PATH/cgroup.procs" 2>/dev/null | head -10 || echo "  (empty)"
            echo ""
            
            # Try to run a test command in the container's cgroup
            echo "--- [6] Test: run command in container cgroup ---"
            echo "Running 'ls /' in cgroup $CGROUP_PATH..."
            sudo cgexec -g cpu,memory:"$CGROUP_REL" ls / 2>/dev/null && echo "  cgexec works" || echo "  cgexec failed (may need cgroup-tools)"
        fi
    fi
else
    echo "--- [5] No running containers ---"
    echo "Starting a test container..."
    sudo docker run -d --name ebpf_test_$$ alpine sleep 60 2>/dev/null || echo "  (docker run failed)"
    CONTAINER_ID=$(sudo docker ps -q --filter "name=ebpf_test_$$" 2>/dev/null | head -1)
    if [ -n "$CONTAINER_ID" ]; then
        echo "Test container: $CONTAINER_ID"
        INIT_PID=$(sudo docker inspect --format '{{.State.Pid}}' "$CONTAINER_ID" 2>/dev/null)
        echo "Init PID: $INIT_PID"
        sudo cat /proc/$INIT_PID/cgroup 2>/dev/null
        # Cleanup
        sudo docker rm -f "$CONTAINER_ID" 2>/dev/null || true
    fi
fi
echo ""

# 7. Check if our BPF program actually captures exec in a container
echo "--- [7] Direct exec in container + bpftrace ---"
CONTAINER_ID=$(sudo docker ps -q 2>/dev/null | head -1)
if [ -n "$CONTAINER_ID" ] && command -v bpftrace &>/dev/null; then
    echo "Monitoring exec events while running 'ls' in container $CONTAINER_ID..."
    sudo timeout 5 bpftrace -e '
        tracepoint:syscalls:sys_enter_execve
        /pid != $BPFTRACE_PID/
        {
            printf("EXEC: comm=%s filename=%s pid=%d\n", comm, str(args->filename), pid);
        }
    ' &
    BPF_PID=$!
    sleep 0.5
    sudo docker exec "$CONTAINER_ID" ls / >/dev/null 2>&1 || true
    sleep 1
    sudo kill $BPF_PID 2>/dev/null || true
    wait $BPF_PID 2>/dev/null || true
    echo ""
    echo "If you saw EXEC events above, bpftrace CAN see container exec."
    echo "If nothing appeared, kprobe/tracepoint cannot see container exec on this kernel."
elif [ -z "$CONTAINER_ID" ]; then
    echo "No running container. Please start one first."
fi
echo ""

# 8. Check BCC Python environment
echo "--- [8] BCC Python test ---"
sudo python3 -c "
from bcc import BPF
bpf = BPF(text='int kprobe__sys_execve(void *ctx) { bpf_trace_printk(\"exec\\\\n\"); return 0; }')
print('BPF program compiled OK')
print('Attached kprobe on sys_execve')
import time
print('Waiting 5 seconds for events...')
time.sleep(5)
print('Done. Check: sudo cat /sys/kernel/debug/tracing/trace_pipe')
" 2>&1 || echo "  BCC test failed"
echo ""

echo "=============================================="
echo "Diagnostic complete."
echo "Send this output back for analysis."
echo "=============================================="
