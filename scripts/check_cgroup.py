"""Check cgroup and BPF for running container 73d29eb198e2"""
import subprocess, sys, time
from pathlib import Path

sys.path.insert(0, "/home/weitian/claw/services/scheduler/src")

CID = "73d29eb198e2"
# Get init PID
r = subprocess.run(["docker", "inspect", CID, "--format", "{{.State.Pid}}"], capture_output=True, text=True)
init_pid = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
print(f"Init PID: {init_pid}")

# Get cgroup
if init_pid > 0:
    cgroup_data = Path(f"/proc/{init_pid}/cgroup").read_text().strip()
    for line in cgroup_data.split("\n"):
        if "0::" in line:
            cgroup_rel = line.split(":")[-1]
            cgroup_path = Path(f"/sys/fs/cgroup{cgroup_rel}")
            cgroup_inode = cgroup_path.stat().st_ino
            print(f"Cgroup path: {cgroup_path}")
            print(f"Cgroup inode: {cgroup_inode}")
            break

# Check sibling cgroups
parent = cgroup_path.parent
name = cgroup_path.name
if name.startswith("docker-"):
    prefix = name[:7+32]
    print(f"\nSibling cgroups with prefix '{prefix}':")
    for entry in sorted(parent.iterdir()):
        if entry.is_dir() and entry.name.startswith(prefix):
            print(f"  {entry.name} inode={entry.stat().st_ino}")

# Check launcher process cgroup
for pid in [2252090, 2252100, 2252160]:
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text().strip()
        for line in cg.split("\n"):
            if "0::" in line:
                print(f"PID {pid} cgroup: {line.split(':')[-1]}")
    except:
        print(f"PID {pid}: not found")

# Now check: what cgroup_inodes would be discovered?
from tool_resource.telemetry import _discover_leaf_cgroup_inodes
inodes = _discover_leaf_cgroup_inodes(cgroup_path)
print(f"\nDiscovered cgroup_inodes: {sorted(inodes)}")

# What cgroup_inodes does the collector actually have?
# The collector has the container's cgroup + sibling cgroups
# These should include the docker exec scope
print(f"\nContainer cgroup inode: {cgroup_inode}")
print(f"Is {cgroup_inode} in discovered inodes?: {cgroup_inode in inodes}")

from tool_resource.telemetry import _add_sibling_cgroup_inodes
inodes2 = {cgroup_inode}
_add_sibling_cgroup_inodes(cgroup_path, inodes2)
print(f"After sibling discovery: {sorted(inodes2)}")

print("\nDone.")
