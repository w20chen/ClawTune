"""Export the exact current source and read-only corpus for both hosts."""
import hashlib
import json
import sys
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
study = Path(__file__).resolve().parent
names = set(json.loads((study.parent / 'kb_online_cost_20261012/source_sha256.json').read_bytes()))
for folder in ('services/sidecar/src', 'research/kb_incremental_20261013'):
    names.update(str(p.relative_to(root)).replace('\\', '/') for p in (root / folder).rglob('*.py'))
manifest = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(names)}
(study / 'source_sha256.json').write_text(json.dumps(manifest, indent=2))
seed = root / '.runtime/offline/kb-methods-fixed6-20261012-r2/seed'
data = {'training.json': seed / 'clause-lattice-time-kb.json',
        'trie.json': seed / 'clause-resource-kb.json',
        'tool.json': seed / 'runtime-tool-resource-kb.json',
        'inputs.json': root / 'research/kb_cost_20261012/inputs.json'}
output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(output, 'w:gz') as archive:
    for name in manifest:
        archive.add(root / name, arcname=name, recursive=False)
    for name, path in data.items():
        archive.add(path, arcname=name, recursive=False)
    archive.add(study / 'source_sha256.json', arcname='source_sha256.json')
print(output, output.stat().st_size)
