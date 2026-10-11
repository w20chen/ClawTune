"""Reproduce into a NEW directory. Source traces remain read-only."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--flat',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,PYTHONHASHSEED='20261011',PYTHONIOENCODING='utf-8')
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for root in (HERE,ROOT/'services/sidecar/src',ROOT/'cold_start')
            for p in root.rglob('*.py') if '__pycache__' not in p.parts}
    manifest=dict(git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  python=sys.version,platform=platform.platform(),code_sha256=hashes,
                  commands=[],source_flat=str(args.flat.resolve()))
    commands=[['prepare.py','--flat',str(args.flat.resolve()),'--output',str((args.output/'data').resolve())],
              ['evaluate.py','--data',str((args.output/'data').resolve()),'--output',str((args.output/'results').resolve())],
              ['audit_quality.py','--data',str((args.output/'data').resolve()),'--output',str((args.output/'results').resolve())],
              ['report.py','--data',str((args.output/'data').resolve()),'--results',str((args.output/'results').resolve())]]
    with (args.output/'execution.log').open('w',encoding='utf-8') as log:
        for command in commands:
            actual=[sys.executable,str(HERE/command[0]),*command[1:]]
            manifest['commands'].append(actual)
            result=subprocess.run(actual,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            log.flush()
            manifest['last_exit_code']=result.returncode
            (args.output/'execution_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
            if result.returncode:raise SystemExit(result.returncode)
    print(args.output.resolve())


if __name__=='__main__':main()
