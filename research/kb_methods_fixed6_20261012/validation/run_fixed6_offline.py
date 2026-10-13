"""Run the functional evaluator unchanged, with progress and source provenance."""
import hashlib
import json
import math
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time
import zipfile
from collections import OrderedDict

from benchmarks.cli import main
from offline import runner
from offline.lattice_oracle import LatticeOracle
from tool_time.lattice_kb import LatticeTimeKB

root=Path.cwd()
archive=root/'research/kb_methods_fixed6_20261012'
archive.mkdir(parents=True,exist_ok=True)
roots=['benchmarks','cold_start','offline','clawtune_kb','services/sidecar/src','contracts']
files=[p for name in roots for p in (root/name).rglob('*')
       if p.is_file() and p.suffix in ('.py','.json') and '__pycache__' not in p.parts]
files += [root/'tools/summarize_offline_buckets.py',Path(__file__)]
hashes={}
with zipfile.ZipFile(archive/'source.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
    for p in sorted(files):
        b=p.read_bytes(); name=p.relative_to(root).as_posix()
        hashes[name]=hashlib.sha256(b).hexdigest()
        z.writestr(name,b)
provenance=dict(command=sys.argv,git_head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
    python=sys.version,python_hash_seed='0',source_sha256=hashes,
    progress_driver='Progress wrappers plus exact memoization of candidate queries on the frozen KB; keys include node-map identity, query features, core, and global standard deviation. No model scores, samples, labels, or predictions altered. Runtime includes this evaluation-only acceleration.')
(archive/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf8')
start=time.monotonic()
status=dict(loaded_tasks=0,oracle_queries=0,phase='loading')
stop=threading.Event()
def heartbeat():
    while not stop.wait(30):
        print('PROGRESS',round(time.monotonic()-start,1),status,flush=True)
threading.Thread(target=heartbeat,daemon=True).start()

load=runner.load_task
def traced_load(*args,**kwargs):
    result=load(*args,**kwargs)
    status['loaded_tasks']+=1
    if status['loaded_tasks']%25==0 or status['loaded_tasks']==515:
        print('LOADED',status['loaded_tasks'],round(time.monotonic()-start,1),flush=True)
    return result
runner.load_task=traced_load

freeze=LatticeTimeKB.freeze
def traced_freeze(kb):
    status['phase']='lattice_build'
    print('LATTICE BUILD start',flush=True)
    result=freeze(kb)
    # All node mappings now stay alive and immutable throughout evaluation.
    # Reuse identical candidate queries between selectors and repeated calls.
    from tool_time import query as query_module, resource_lattice
    from tool_time import lattice_kb as lattice_module
    original_query=query_module.query_nodes
    cache=OrderedDict()
    status['candidate_cache_hits']=status['candidate_cache_misses']=0
    def cached_query(nodes,query,*,global_log_std):
        key=(id(nodes),query.features,query.core,global_log_std)
        if key in cache:
            status['candidate_cache_hits']+=1
            cache.move_to_end(key)
            return cache[key]
        status['candidate_cache_misses']+=1
        value=original_query(nodes,query,global_log_std=global_log_std)
        cache[key]=value
        if len(cache)>2048:
            cache.popitem(last=False)
        return value
    resource_lattice.query_nodes=lattice_module.query_nodes=cached_query
    print('LATTICE BUILD end',round(time.monotonic()-start,1),len(kb._resource_states['load:duration_ms'].nodes),flush=True)
    status['phase']='evaluation'
    return result
LatticeTimeKB.freeze=traced_freeze

candidates=LatticeTimeKB.duration_candidate_samples
def traced_candidates(*args,**kwargs):
    result=candidates(*args,**kwargs)
    counts=[len(r) for r in result]
    status['oracle_candidate_counts']=counts
    status['oracle_combinations']=math.prod(counts)
    if math.prod(counts)>10000:
        print('LARGE ORACLE',status['oracle_queries'],counts,math.prod(counts),flush=True)
    return result
LatticeTimeKB.duration_candidate_samples=traced_candidates

oracle=LatticeOracle.predict_load_samples
def traced_oracle(self,*args,**kwargs):
    status['oracle_queries']+=1
    begin=time.monotonic()
    result=oracle(self,*args,**kwargs)
    elapsed=time.monotonic()-begin
    if elapsed>3:
        print('ORACLE DONE',status['oracle_queries'],round(elapsed,2),flush=True)
    return result
LatticeOracle.predict_load_samples=traced_oracle

try:
    code=main()
finally:
    stop.set()
    provenance.update(elapsed_seconds=time.monotonic()-start,
                      peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
                      final_progress=status)
    (archive/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf8')
raise SystemExit(code)
