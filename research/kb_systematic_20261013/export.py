"""Archive the current workspace and existing KB; never package credentials."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
seed = ROOT / '.runtime/offline/kb-methods-fixed6-20261012-r2/seed'
files = subprocess.check_output(['git', 'ls-files', '-co', '--exclude-standard'], cwd=ROOT).decode().splitlines()
prefixes = ('services/sidecar/src/', 'services/sidecar/tests/', 'offline/', 'cold_start/',
            'benchmarks/', 'clawtune_kb/', 'contracts/', 'tests/', 'scripts/command_rules/', 'swe_rebench/',
            'research/kb_systematic_20261013/')
files = sorted({f for f in files if f.startswith(prefixes) and not any(
    s in f for s in ('api_key', '__pycache__', 'node_modules', '.zip', '.gz'))
    and (ROOT / f).is_file()})
manifest = {}
with zipfile.ZipFile(OUT / 'source.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for f in files:
        data = (ROOT / f).read_bytes()
        z.writestr(f, data)
        manifest[f] = hashlib.sha256(data).hexdigest()
with zipfile.ZipFile(OUT / 'baseline.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(seed.glob('*.json')):
        z.write(p, 'baseline/' + p.name)
    z.write(seed.parent / 'split.json', 'baseline/original-split.json')
meta = {'git_head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
        'source_files': manifest, 'baseline_source': str(seed),
        'baseline_files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in seed.glob('*.json')},
        'workspace_status_before': subprocess.check_output(['git','status','--short'],cwd=ROOT).decode()}
(OUT / 'export.json').write_text(json.dumps(meta, indent=2), encoding='utf8')
print('archived', len(files), 'source files; existing seed, no training performed')
