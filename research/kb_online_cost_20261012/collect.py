"""Export small timing evidence, excluding large generated KB snapshots."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument('--root', type=Path, required=True)
p.add_argument('--results', type=Path, required=True)
p.add_argument('--dest', type=Path, required=True)
a = p.parse_args()
a.dest.mkdir(parents=True, exist_ok=True)
for path in a.results.iterdir():
    if path.is_file() and path.suffix in {'.json', '.jsonl', '.log'} and not path.name.endswith(('-trained.json', '-snapshot.json')):
        shutil.copy2(path, a.dest / path.name)
files = sorted([*a.root.glob('services/sidecar/src/**/*.py'),
                *a.root.glob('services/sidecar/src/**/*.json'),
                *a.root.glob('contracts/**/*.json'),
                *a.root.glob('research/kb_online_cost_20261012/*.py')])
(a.dest / 'source_sha256.json').write_text(json.dumps({
    str(path.relative_to(a.root)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in files}, indent=2))
packages = {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}
metadata = {'python': sys.version, 'platform': platform.platform(), 'packages': packages,
            'lscpu': subprocess.check_output(['lscpu'], text=True),
            'filesystem': subprocess.check_output(['df', '-T', str(a.root)], text=True)}
(a.dest / 'environment.json').write_text(json.dumps(metadata, indent=2))
print('exported', len(list(a.dest.iterdir())), 'evidence files')
