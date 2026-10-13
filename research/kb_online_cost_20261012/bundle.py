"""Create an isolated, reproducible study bundle from read-only local inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

base = Path(__file__).resolve().parents[2]
study = Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument('output', type=Path)
a = p.parse_args()
manifest = json.loads((study / 'source_sha256.json').read_bytes())
seed = base / '.runtime/offline/kb-methods-fixed6-20261012-r2/seed'
data = {'training.json': seed / 'clause-lattice-time-kb.json',
        'trie.json': seed / 'clause-resource-kb.json',
        'tool.json': seed / 'runtime-tool-resource-kb.json',
        'inputs.json': base / 'research/kb_cost_20261012/inputs.json'}
a.output.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(a.output, 'w:gz') as archive:
    for name, expected in manifest.items():
        path = base / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
        archive.add(path, arcname=name, recursive=False)
    for name, path in data.items():
        archive.add(path, arcname=name, recursive=False)
print(a.output, a.output.stat().st_size)
