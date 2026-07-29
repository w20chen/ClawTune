"""Deep debug: trace exec event flow for docker exec commands."""
import subprocess, sys, time, threading, tempfile
from pathlib import Path

print("=== Setup ===")
result = subprocess.run(
    ["docker", "ps", "-q", "--filter", "name=claw-srb"],
    capture_output=True, text=True,
)
cids = [c for c in result.stdout.strip().split("\n") if c]
cid = cids[0] if cids else ""
if not cid:
    result = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", "claw-srb-dbg", "alpine", "sleep", "600"],
        capture_output=True, text=True,
    )
    cid = result.stdout.strip()
print(f"Container: {cid}")

result = subprocess.run(
    ["docker", "inspect", cid, "--format", "{{.State.Pid}}"],
    capture_output=True, text=True,
)
init_pid = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0

cgroup_data = Path(f"/proc/{init_pid}/cgroup").read_text().strip()
cgroup_rel = None
for line in cgroup_data.split("\n"):
    if "0::" in line:
        cgroup_rel = line.split(":")[-1]
        break
cgroup_path = Path(f"/sys/fs/cgroup{cgroup_rel}") if cgroup_rel else None
print(f"Init PID: {init_pid}, Cgroup: {cgroup_path}")

sys.path.insert(0, "/home/weitian/claw/services/scheduler/src")
from tool_resource.telemetry import ClauseTelemetryCollector, _container_pid_set

tmpdir = Path(tempfile.mkdtemp(prefix="claw_dbg_"))
artifact_path = tmpdir / "artifacts"
artifact_path.mkdir(parents=True, exist_ok=True)

collector = ClauseTelemetryCollector(
    container_id=cid, container_executable="docker",
    repo="openclaw", artifact_path=artifact_path,
    cgroup_path=str(cgroup_path),
)
print(f"Collector: state={collector.state}, cgroup_inodes={sorted(collector.cgroup_inodes)}")
time.sleep(0.5)

# Test: docker exec
print("\n=== Test: docker exec ===")
token = collector.begin_tool_call("test1", "echo hello_world")
subprocess.run(["docker", "exec", cid, "/bin/sh", "-c", "echo hello_world"], capture_output=True, timeout=10)
time.sleep(0.5)
summary = collector.finish_tool_call(token)
print(f"Quality: {summary.get('telemetry_quality')}, Clauses: {len(summary.get('clauses',[]))}")
if summary.get('invalid_reasons'):
    for r in summary['invalid_reasons']:
        print(f"  Reason: {r['detail'][:300]}")

# What events do we have?
exec_pids = set()
cgids = set()
for e in collector._events:
    cgids.add(e.get("cgroup_id", 0))
    if e.get("type") == "exec_boundary":
        exec_pids.add(e.get("host_pid"))
evt_types = {}
for e in collector._events:
    evt_types[e.get("type")] = evt_types.get(e.get("type"), 0) + 1

print(f"\nTotal events: {len(collector._events)}, types: {evt_types}")
print(f"Exec PIDs: {sorted(exec_pids)}")
print(f"All cgroup_ids: {sorted(cgids)}")
print(f"Collector cgroup_inodes: {sorted(collector.cgroup_inodes)}")

pids_cg = _container_pid_set(collector._events, collector.init_pid, cgroup_inodes=collector.cgroup_inodes)
pids_leg = _container_pid_set(collector._events, collector.init_pid)
print(f"Container PIDs (cgroup): {sorted(pids_cg)}")
print(f"Container PIDs (legacy): {sorted(pids_leg)}")

# Detailed exec events
for e in collector._events:
    if e.get("type") in ("exec_boundary", "exec_meta", "bprm_meta", "fork"):
        print(f"  [{e.get('type'):16s}] pid={e.get('host_pid'):7d} child={e.get('child_host_pid',0):7d} cgroup={e.get('cgroup_id')} parent={e.get('parent_host_pid',0):7d} arg={e.get('arg','')[:60]}")

collector.finalize()
import shutil; shutil.rmtree(tmpdir, ignore_errors=True)
print("\n=== DONE ===")
