"""Evaluation-only exact scorer; final predictions still use production compose."""
import ctypes
from functools import lru_cache
from pathlib import Path
import random
from types import SimpleNamespace
from clawtune_sidecar.predictors.call_load import compose as reference, _composition_groups

_double_p=ctypes.POINTER(ctypes.c_double)
_int_p=ctypes.POINTER(ctypes.c_int)
_native=ctypes.CDLL(str(Path(__file__).with_name('_oracle_native_kernel.so'))).oracle_p50
_native.argtypes=[ctypes.POINTER(_double_p),_int_p,ctypes.c_int,_int_p,_int_p,ctypes.c_int,ctypes.POINTER(ctypes.c_uint32)]
_native.restype=ctypes.c_double
_state=(ctypes.c_uint32*625)(*random.Random(0).getstate()[1])

@lru_cache(maxsize=1024)
def _array(values):
    return (ctypes.c_double*len(values))(*values)

def compose(backend,evidence,edges,*,reason=None,clauses=()):
    rows=[row.get('duration_ms') for row in evidence]
    if reason or len(rows)<2 or any(not r or not r.get('values') or r.get('weights') is not None for r in rows):
        return reference(backend,evidence,edges,reason=reason,clauses=clauses)
    groups=_composition_groups(clauses,len(rows))
    if groups is None:
        return reference(backend,evidence,edges,reason=reason,clauses=clauses)
    arrays=[_array(tuple(r['values'])) for r in rows]
    sizes=[len(a) for a in arrays]
    if any(n>=2**31 for n in sizes):
        return reference(backend,evidence,edges,reason=reason,clauses=clauses)
    group_sizes=[len(g) for g in groups]
    members=[i for g in groups for i in g]
    p50=_native((_double_p*len(rows))(*arrays),(ctypes.c_int*len(rows))(*sizes),len(rows),
        (ctypes.c_int*len(groups))(*group_sizes),(ctypes.c_int*len(members))(*members),len(groups),_state)
    return SimpleNamespace(targets={'duration_ms':SimpleNamespace(status='available',p50=p50)})
