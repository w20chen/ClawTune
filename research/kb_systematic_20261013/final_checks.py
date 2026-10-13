"""Record environment, file integrity, and a non-disclosing credential scan."""
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from research.kb_systematic_20261013.study import HERE,read,write,digest

def main():
    out=HERE/'results'
    write(out/'environment.json',{'platform':platform.platform(),'python':sys.version,
        'packages':{p:importlib.metadata.version(p) for p in ('numpy','httpx','pytest','jsonschema','pydantic','ruff')},
        'cpu':json.loads(subprocess.check_output(['lscpu','-J'])),
        'source_root':str(Path.cwd()),'dataset':read(out/'inventory.json')['dataset']})
    original=read(out/'freeze-verification.json')['files_before']
    current={p.name:digest(p) for p in Path('baseline').glob('*.json')}
    assert original==current
    inventory=read(out/'inventory.json');dataset=Path(inventory['dataset'])
    source_files={f['path']:f['sha256'] for t in inventory['tasks'].values() for f in t['files']}
    dataset_changes=[name for name,expected in source_files.items() if digest(dataset/name)!=expected]
    assert not dataset_changes
    key=Path(os.environ.get('CLAWTUNE_STUDY_KEY_FILE','/home/weitianc/ClawTune/swe_rebench/llm_api_key.txt')).read_bytes().strip()
    checked=0;matches=[]
    for path in HERE.rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix not in ('.json','.py','.md','.sh','.log','.gz'):continue
        # Binary model snapshots contain no external HTTP inputs, but scan their decompressed bytes too.
        if path.suffix=='.gz':
            with gzip.open(path,'rb') as stream:
                tail=b''
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    if key in tail+chunk:matches.append(str(path.relative_to(HERE)));break
                    tail=chunk[-len(key):]
        elif key in path.read_bytes():matches.append(str(path.relative_to(HERE)))
        checked+=1
    assert not matches, 'credential content detected; inspect only paths, never print key'
    write(out/'final-integrity.json',{'baseline_unchanged':original==current,'baseline_sha256':current,
        'dataset_rehashed_files':len(source_files),'dataset_changed_files':dataset_changes,
        'credential_files_checked':checked,'credential_matches':matches})

if __name__=='__main__':main()
