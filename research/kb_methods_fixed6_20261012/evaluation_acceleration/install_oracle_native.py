"""Attach a verified evaluation-only scorer without changing search order."""
import sys as native_sys
import json as native_json
import random as native_random
import time as native_time
import hashlib as native_hashlib
from pathlib import Path as NativePath
import __main__ as native_main
import oracle_native as native_module
import offline.lattice_oracle as native_target

native_verified=0
for native_frame in native_sys._current_frames().values():
    while native_frame:
        if native_frame.f_code.co_filename.endswith('offline/lattice_oracle.py') and native_frame.f_code.co_name=='predict_load_samples':
            native_local=native_frame.f_locals
            native_rng=native_random.Random(814)
            for native_case in range(128):
                native_choice=tuple(native_rng.choice(rows) for rows in native_local['candidates'])
                native_evidence=native_local['evidence'](native_choice)
                native_args=('lattice',native_evidence,native_local['self'].edges)
                native_kwargs={'clauses':native_local['clauses']}
                native_expected=native_module.reference(*native_args,**native_kwargs).targets['duration_ms']
                native_actual=native_module.compose(*native_args,**native_kwargs).targets['duration_ms']
                assert (native_expected.status,native_expected.p50)==(native_actual.status,native_actual.p50)
                native_verified+=1
        native_frame=native_frame.f_back
assert native_verified==128,native_verified
native_state={'calls':0,'checked':native_verified}
def native_checked_compose(*args,**kwargs):
    result=native_module.compose(*args,**kwargs)
    native_state['calls']+=1
    if native_state['calls']<=64 or native_state['calls']%4096==0:
        expected=native_module.reference(*args,**kwargs).targets['duration_ms']
        actual=result.targets['duration_ms']
        assert (actual.status,actual.p50)==(expected.status,expected.p50)
        native_state['checked']+=1
    return result
native_main.provenance['native_oracle_acceleration']={
    'activation_query':native_main.status['oracle_queries'],
    'activation_elapsed_seconds':native_time.monotonic()-native_main.start,
    'validation':native_json.loads(NativePath('.runtime/oracle-native-validation.json').read_text()),
    'live_validation':native_state,
    'scope':'oracle internal p50 scoring only; identical Random(0) draws, 2048 samples and grouping; exhaustive order and final production composer unchanged',
    'compiler':'ziglang 0.17.0 cc -shared -fPIC -O2 -ffp-contract=off -fno-fast-math',
    'files_sha256':{name:native_hashlib.sha256(NativePath('.runtime',name).read_bytes()).hexdigest()
        for name in ('oracle_native.c','oracle_native.py','_oracle_native_kernel.so','validate_oracle_native.py','install_oracle_native.py')},
}
native_main.status['native_oracle_scoring']=native_state
native_target.compose=native_checked_compose
print('NATIVE ORACLE ENABLED',native_main.provenance['native_oracle_acceleration'],flush=True)
