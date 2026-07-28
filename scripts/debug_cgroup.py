"""Live debug: check cgroup flow for BPF exec capture."""
import subprocess
import os
import sys
from pathlib import Path

print("=== 1. Find running Docker container ===")
result = subprocess.run(
    ["docker", "ps", "-q", "--filter", "name=claw-srb"],
    capture_output=True, text=True,
)
cids = [c for c in result.stdout.strip().split("\n") if c]
cid = cids[0] if cids else ""
if not cid:
    print("No claw-srb container, starting test...")
    result = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", "claw-srb-debug", "alpine", "sleep", "300"],
        capture_output=True, text=True,
    )
    cid = result.stdout.strip()
print(f"Container ID: {cid}")

print("\n=== 2. Get init PID and cgroup ===")
result = subprocess.run(
    ["docker", "inspect", cid, "--format", "{{.State.Pid}}"],
    capture_output=True, text=True,
)
init_pid = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
print(f"Init PID: {init_pid}")

# Read cgroup from /proc
cgroup_data = Path(f"/proc/{init_pid}/cgroup").read_text().strip()
print(f"/proc/{init_pid}/cgroup:")
print(cgroup_data)

cgroup_path = None
cgroup_inode = 0
for line in cgroup_data.split("\n"):
    if "0::" in line:
        cgroup_rel = line.split(":")[-1]
        cgroup_path = Path(f"/sys/fs/cgroup{cgroup_rel}")
        print(f"\nCgroup path: {cgroup_path}")
        print(f"Exists: {cgroup_path.is_dir()}")
        if cgroup_path.is_dir():
            cgroup_inode = cgroup_path.stat().st_ino
            print(f"Inode: {cgroup_inode}")
            procs = (cgroup_path / "cgroup.procs").read_text().strip()
            print(f"Procs in cgroup: {procs}")
        break

print("\n=== 3. Test _container_pid_set with discovered cgroup_inodes ===")
sys.path.insert(0, "/home/weitian/claw/services/scheduler/src")
from tool_resource.telemetry import _container_pid_set

if cgroup_inode > 0:
    # Simulate real scenario
    events = [
        {"host_pid": init_pid, "cgroup_id": cgroup_inode, "type": "exec_boundary", "parent_host_pid": 1},
        {"host_pid": init_pid, "cgroup_id": cgroup_inode, "type": "fork", "child_host_pid": init_pid + 100, "parent_host_pid": 1},
        {"host_pid": init_pid + 100, "cgroup_id": cgroup_inode, "type": "exec_boundary", "parent_host_pid": init_pid},
    ]
    result = _container_pid_set(events, init_pid, cgroup_inodes={cgroup_inode})
    print(f"With cgroup_inodes: {sorted(result)}")
    expected = {init_pid, init_pid + 100}
    print(f"Expected: {sorted(expected)}")
    print(f"Match: {result == expected}")

    result2 = _container_pid_set(events, init_pid)
    print(f"Without cgroup_inodes (legacy): {sorted(result2)}")

print("\n=== 4. Test _discover_leaf_cgroup_inodes (with sibling fix) ===")
from tool_resource.telemetry import _discover_leaf_cgroup_inodes, _add_sibling_cgroup_inodes
if cgroup_path and cgroup_path.is_dir():
    inodes = _discover_leaf_cgroup_inodes(cgroup_path)
    print(f"Discovered inodes (with siblings): {sorted(inodes)}")
    print(f"Count: {len(inodes)}")
    
    # Also show the sibling cgroups found
    parent = cgroup_path.parent
    name = cgroup_path.name
    if name.startswith("docker-"):
        prefix = name[:7+32]
        print(f"Looking for siblings with prefix '{prefix}' in {parent}")
        for entry in sorted(parent.iterdir()):
            if entry.is_dir() and entry.name.startswith(prefix):
                ino = entry.stat().st_ino
                marker = " <-- CONTAINER" if entry == cgroup_path else " <-- SIBLING"
                print(f"  {entry.name} (inode={ino}){marker}")
    
    # Also show all docker- entries in the parent for context
    print(f"\nAll docker-* entries in {parent}:")
    for entry in sorted(parent.iterdir()):
        if entry.is_dir() and entry.name.startswith("docker-"):
            ino = entry.stat().st_ino
            # Show which match our prefix
            matched = entry.name.startswith(prefix)
            print(f"  {entry.name} (inode={ino}) matched_prefix={matched}")

print("\n=== 5. Live BPF capture test: run exec in container ===")
# Attach a ClauseTelemetryCollector to the running container
# and capture events from a simple docker exec
from tool_resource.telemetry import ClauseTelemetryCollector
import tempfile, time, threading

# Create temp artifact path
tmpdir = Path(tempfile.mkdtemp(prefix="claw_debug_"))
artifact_path = tmpdir / "artifacts"
artifact_path.mkdir(parents=True, exist_ok=True)

print(f"Artifact path: {artifact_path}")

collector = None
try:
    collector = ClauseTelemetryCollector(
        container_id=cid,
        container_executable="docker",
        repo="openclaw",
        artifact_path=artifact_path,
        cgroup_path=str(cgroup_path),
    )
    print(f"Collector state: {collector.state}")
    print(f"Collector cgroup_inodes: {sorted(collector.cgroup_inodes)}")
    print(f"Collector cgroup_id: {collector.cgroup_id}")
    print(f"Collector init_pid: {collector.init_pid}")

    # Wait a bit for BPF to arm
    time.sleep(0.5)

    # Start a tool call and run docker exec
    token = collector.begin_tool_call("debug_test_001", "/bin/sh -c 'echo hello_from_container'")
    print(f"\nToken command: {token.command}")

    # Run docker exec
    result = subprocess.run(
        ["docker", "exec", cid, "/bin/sh", "-c", "echo hello_from_container"],
        capture_output=True, text=True, timeout=10,
    )
    print(f"Docker exec stdout: {result.stdout.strip()}")
    print(f"Docker exec exit: {result.returncode}")

    # Small delay for BPF drain
    time.sleep(0.5)

    # Finish and get summary
    summary = collector.finish_tool_call(token)
    print(f"\n=== Telemetry Summary ===")
    print(f"Quality: {summary.get('telemetry_quality')}")
    print(f"Clause count: {len(summary.get('clauses', []))}")
    if summary.get('clauses'):
        for c in summary['clauses']:
            print(f"  Clause: pid={c.get('host_pid')}, bin={c.get('bin')}, argv={c.get('argv', [])[:3]}")
    if summary.get('invalid_reasons'):
        for r in summary['invalid_reasons']:
            print(f"  Invalid: {r['detail'][:200]}")

    # Check raw events
    print(f"\nTotal raw events captured: {len(collector._events)}")
    event_types = {}
    exec_pids = set()
    for e in collector._events:
        t = e.get("type", "unknown")
        event_types[t] = event_types.get(t, 0) + 1
        if t == "exec_boundary":
            exec_pids.add(e.get("host_pid"))
    print(f"Event types: {event_types}")
    print(f"Exec PIDs: {sorted(exec_pids)}")
    if exec_pids:
        for e in collector._events:
            if e.get("type") == "exec_boundary":
                print(f"  exec: pid={e.get('host_pid')}, cgroup={e.get('cgroup_id')}, parent={e.get('parent_host_pid')}, seq={e.get('exec_seq')}")
                break

    # Check container_pids
    from tool_resource.telemetry import _container_pid_set
    pids_legacy = _container_pid_set(collector._events, collector.init_pid)
    pids_cgroup = _container_pid_set(collector._events, collector.init_pid, cgroup_inodes=collector.cgroup_inodes)
    print(f"\nContainer PIDs (legacy): {sorted(pids_legacy)}")
    print(f"Container PIDs (cgroup): {sorted(pids_cgroup)}")

finally:
    if collector is not None:
        collector.finalize()
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

print("\n=== DONE ===")
