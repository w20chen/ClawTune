"""Hash every node field on the real corpus, in separate memory-isolated runs.

Run reference mode with the BEFORE-repair source bundle on PYTHONPATH, and
incremental mode with the repaired bundle. Numeric values and node order must
agree exactly; the reference implementation is not imported from the new tree.
"""
import argparse
import hashlib
import json
import time
from dataclasses import fields
from pathlib import Path

from tool_resource.runtime_kb import ClauseObservation
from tool_time._lattice_vendor.schemas import NodeStats
from tool_time.lattice_kb import LatticeTimeKB

p = argparse.ArgumentParser()
p.add_argument('mode', choices=('reference', 'incremental'))
p.add_argument('--root', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
p.add_argument('--limit', type=int, default=6)
a = p.parse_args()


def observation(data, index=None):
    values = dict(data, argv=tuple(data['argv']))
    if index is not None:
        values.update(ts_start=3e12 + index * 1000, ts_end=3e12 + index * 1000 + 1)
    return ClauseObservation(**values)


columns = [f.name for f in fields(NodeStats) if f.name != 'features']


def fingerprint(nodes):
    digest = hashlib.sha256()
    for key, node in nodes.items():
        value = [sorted(key), *[getattr(node, field) for field in columns]]
        digest.update(json.dumps(value, separators=(',', ':')).encode())
        digest.update(b'\n')
    return {'nodes': len(nodes), 'sha256': digest.hexdigest()}


started = time.perf_counter()
training = (a.root / 'training.json').read_bytes()
inputs = (a.root / 'inputs.json').read_bytes()
rows = [observation(row) for row in json.loads(training)['observations']]
updates = [observation(row, i) for i, row in enumerate(json.loads(inputs)['update_clauses'])]
if a.mode == 'reference':
    kb = LatticeTimeKB.fit([*rows, *updates], max_optional_features=a.limit)
    kb.prepare()
else:
    kb = LatticeTimeKB.fit(rows, max_optional_features=a.limit)
    kb.prepare()
    kb = kb.fork_for_update()
    for row in updates:
        kb.observe_completed_clause(row)
    kb.prepare()
    kb._advance(4e12)
print('ready', a.mode, round(time.perf_counter() - started, 2), flush=True)
result = {'corpus_sha256': hashlib.sha256(training).hexdigest(),
          'inputs_sha256': hashlib.sha256(inputs).hexdigest(), 'columns': columns,
          'global': [kb._global_log_var, kb._global_log_std, kb._global_median_s],
          'time': fingerprint(kb._nodes), 'resources': {}}
seen = {}
for name, state in kb._resource_states.items():
    if id(state) not in seen:
        seen[id(state)] = fingerprint(state.nodes)
    result['resources'][name] = {'global': [state.global_log_var, state.global_log_std],
                                 **seen[id(state)]}
    print(name, result['resources'][name]['nodes'], flush=True)
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps(result, indent=2))
print('done', a.mode, round(time.perf_counter() - started, 2), flush=True)
