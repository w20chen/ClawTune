"""Serial isolated measurements; resource ceilings apply only to this experiment."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

p = argparse.ArgumentParser()
p.add_argument('--root', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
p.add_argument('--cases', default='lattice:6:360,edge:6:900,lattice:2:180,edge:2:180,pair:2:300')
p.add_argument('--rss-gib', type=float, default=12)
args = p.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
for case in args.cases.split(','):
    model, limit, ceiling = case.split(':')
    name = f'{model}-{limit}'
    command = [sys.executable, str(Path(__file__).with_name('probe.py')), model, limit,
               '--root', str(args.root), '--out', str(args.out)]
    with (args.out / (name + '.log')).open('w') as log:
        start = time.monotonic()
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        reason = None
        while proc.poll() is None:
            try:
                rss = psutil.Process(proc.pid).memory_info().rss
            except psutil.NoSuchProcess:
                break
            if time.monotonic() - start > float(ceiling):
                reason = 'experiment_time_ceiling'
            if rss > args.rss_gib * 2**30:
                reason = 'experiment_memory_ceiling'
            if reason:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
                break
            time.sleep(1)
    result = {'case': case, 'returncode': proc.wait(), 'termination': reason,
              'wall_s': time.monotonic() - start}
    (args.out / (name + '-supervisor.json')).write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
