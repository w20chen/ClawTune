#!/bin/bash
set -x
echo "=== KERNEL ==="
uname -r

echo "=== CGROUP ==="
cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || echo "no unified cgroup"

echo "=== DOCKER CGROUP ==="
docker info 2>/dev/null | grep -i 'cgroup' | head -10

echo "=== SYSCALL_WRAPPER ==="
zcat /proc/config.gz 2>/dev/null | grep SYSCALL_WRAPPER || echo "not found in /proc/config.gz"
grep SYSCALL_WRAPPER /boot/config-$(uname -r) 2>/dev/null || echo "not found in /boot/config"

echo "=== KPROBE CONFIG ==="
zcat /proc/config.gz 2>/dev/null | grep -E 'KPROBE|BPF_SYSCALL|FTRACE_SYSCALL|HAVE_EBPF|ARCH_SUPPORTS' | head -20

echo "=== DOCKER SECCOMP ==="
docker info 2>/dev/null | grep -i 'seccomp\|Security'

echo "=== DOCKER CGROUPNS ==="
docker run --rm alpine cat /proc/self/cgroup 2>/dev/null | head -5

echo "=== BPF TOOLS ==="
which bpftool && bpftool version 2>/dev/null
which python3 && python3 --version

echo "=== DOCKER CONTAINER CGROUP ==="
docker inspect $(docker ps -q | head -1) 2>/dev/null | grep -A5 Cgroup

echo "=== MOUNTS ==="
mount | grep cgroup | head -5

echo "=== CHECK BCC ==="
python3 -c "from bcc import BPF; print('BCC OK')" 2>&1

echo "=== DONE ==="
