"""Derive the same timing probe with exact state/output equivalence checks."""
from pathlib import Path

root = Path(__file__).parent
source = (root / 'baseline_worker.py').read_text()
source = source.replace("out=Path('research/kb_cost_20261012')", "out=Path(sys.argv[2]); out.mkdir(parents=True,exist_ok=True)")
source = source.replace("(out/'inputs.json')", "(Path('research/kb_cost_20261012')/'inputs.json')")
source = source.replace("print('READY',metadata,flush=True)", '''metadata['ready_peak_rss_mib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
print('READY',metadata,flush=True)
phase['stage']='fingerprint'
def fingerprint(nodes):
    digest=hashlib.sha256()
    for features,node in nodes.items():
        values={field.name:getattr(node,field.name) for field in dataclasses.fields(node)}
        values['features']=sorted(features)
        digest.update(json.dumps(values,sort_keys=True,separators=(',',':')).encode())
        digest.update(b'\\n')
    return dict(count=len(nodes),sha256=digest.hexdigest())
metadata['state_fingerprints']={'legacy':fingerprint(kb._nodes)}
metadata['state_fingerprints'].update({key:{**fingerprint(state.nodes),
    'global_log_var':state.global_log_var,'global_log_std':state.global_log_std}
    for key,state in kb._resource_states.items()})
metadata['legacy_globals']=[kb._global_log_var,kb._global_log_std,kb._global_median_s]
(out/'fingerprints.json').write_text(json.dumps(metadata,indent=2))
print('FINGERPRINTED',flush=True)''')
source = source.replace("match_ms=match_ms,compose_ms=compose_ms,status=", "evidence=evidence,targets={k:v.model_dump(mode='json') for k,v in prediction.items()},match_ms=match_ms,compose_ms=compose_ms,status=")
source = source.replace("phase['stage']='maintenance'", '''phase['stage']='duration_only'
duration_times=[]
states=kb._resource_states
kb._resource_states={'load:duration_ms':states['load:duration_ms']}
for row in samples:
    _,ms=timed(kb.predict_load_samples,row['repo'],row['clauses'],1e12)
    duration_times.append(ms)
kb._resource_states=states
metadata['duration_only_match']=stats(duration_times)
phase['stage']='maintenance' ''')
source = source.replace("maintenance=dict(fork_ms=fork_ms", """if '--prepare-update' in sys.argv:
        phase['stage']='prepare_update'
        _,ms=timed(successor.prepare)
        metadata['update_prepare_ms']=ms
        _,ms=timed(successor._advance,3e12)
        metadata['update_publish_ms']=ms
    maintenance=dict(fork_ms=fork_ms""")
(root / 'worker.py').write_text(source)
