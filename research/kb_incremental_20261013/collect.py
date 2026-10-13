"""Copy small measurements, excluding large trained models and snapshots."""
import argparse
import shutil
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('source', type=Path)
p.add_argument('destination', type=Path)
a = p.parse_args()
a.destination.mkdir(parents=True, exist_ok=True)
for path in a.source.iterdir():
    if not path.is_file() or path.suffix not in ('.json', '.jsonl', '.log'):
        continue
    if path.name.endswith(('-snapshot.json', '-trained.json', '-stream.json')):
        continue
    shutil.copyfile(path, a.destination / path.name)
