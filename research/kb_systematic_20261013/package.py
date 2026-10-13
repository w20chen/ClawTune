"""Package source and hash artifacts; does not regenerate baseline archives."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import zipfile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def source():
    original=json.loads((HERE/'export.json').read_text(encoding='utf8'))
    files=set(original['source_files'])
    files.update(str(p.relative_to(ROOT)).replace('\\','/') for p in HERE.iterdir() if p.suffix in ('.py','.sh'))
    files.update(p for p in ('pyproject.toml','services/sidecar/pyproject.toml','services/sidecar/pytest.ini',
        'tools/summarize_offline_buckets.py') if (ROOT/p).is_file())
    files={f for f in files if (ROOT/f).is_file()}
    manifest={}
    with zipfile.ZipFile(HERE/'final-source.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            data=(ROOT/name).read_bytes();z.writestr(name,data);manifest[name]=hashlib.sha256(data).hexdigest()
    (HERE/'final-source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    changed=['contracts/command-rule.schema.json','services/sidecar/src/command_parser/generation.py',
        'services/sidecar/tests/test_command_rule_generation.py','services/sidecar/tests/test_command_help_isolation.py']
    patch=[]
    with zipfile.ZipFile(HERE/'source.zip') as old:
        for f in changed:
            a=[line+'\n' for line in old.read(f).decode().splitlines()]
            b=[line+'\n' for line in (ROOT/f).read_text(encoding='utf8').splitlines()]
            patch.extend(difflib.unified_diff(a,b,fromfile='a/'+f,tofile='b/'+f))
    (HERE/'production-fix.patch').write_text(''.join(patch),encoding='utf8')

def manifest():
    entries={}
    for p in sorted(HERE.rglob('*')):
        if p.is_file() and p.name!='artifact-manifest.json' and '__pycache__' not in p.parts and p.suffix not in ('.tar','.tgz'):
            entries[str(p.relative_to(HERE)).replace('\\','/') ]={'bytes':p.stat().st_size,'sha256':sha(p)}
    (HERE/'artifact-manifest.json').write_text(json.dumps(entries,indent=2)+'\n',encoding='utf8')
    print('artifacts',len(entries),'bytes',sum(x['bytes'] for x in entries.values()))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=('source','manifest'));a=p.parse_args()
    source() if a.phase=='source' else manifest()
