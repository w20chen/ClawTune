"""Serial trials with explicit time/RSS ceilings, never silent truncation."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import psutil

p = argparse.ArgumentParser()
p.add_argument("--root", type=Path, required=True)
p.add_argument("--out", type=Path, required=True)
p.add_argument("--cases", default="lattice:6:360,edge:6:180,lattice:2:180,edge:2:240,lattice:1:120,edge:1:180,pair:1:180,pair:2:240,edge-cold:6:180")
args = p.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
for specification in args.cases.split(","):
    model, limit, timeout = specification.split(":")
    name = f"{model}-{limit}"
    if model == "pair" and not (args.out / f"edge-{limit}-trained.json").exists():
        continue
    env = dict(os.environ, PYTHONHASHSEED="42", PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, str(Path(__file__).with_name("study.py")), model, limit,
               "--root", str(args.root), "--out", str(args.out)]
    with (args.out / (name + ".log")).open("w") as log:
        start = time.monotonic()
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        maximum = 0
        termination = None
        while proc.poll() is None:
            try:
                memory = psutil.Process(proc.pid).memory_info().rss
                maximum = max(maximum, memory)
            except psutil.NoSuchProcess:
                break
            if time.monotonic()-start > float(timeout):
                termination = "time_budget_exceeded"
            elif memory > 12 * 2**30:
                termination = "study_rss_ceiling_12GiB"
            if termination:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                break
            time.sleep(1)
    result = dict(model=model, limit=int(limit), returncode=proc.wait(), termination=termination,
                  wall_s=time.monotonic()-start, observed_peak_rss_mib=maximum/2**20,
                  trial_time_budget_s=float(timeout))
    (args.out / (name + "-supervisor.json")).write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
