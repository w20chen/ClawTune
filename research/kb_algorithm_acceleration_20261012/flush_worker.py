"""Time the actual single-writer flush, including reconstruction and checkpoint.

Only a temporary writable state is modified; the seed and traces are read-only.
"""
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

from clawtune_kb import StateStore, initialize_state
from clawtune_kb.store import committed_state
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
from tool_resource.runtime_kb import (
    ClauseObservation,
    ClauseResourceKB,
    LatencyBuckets,
    RuntimeToolResourceKB,
)
from tool_time.lattice_kb import LatticeTimeKB

out = Path(sys.argv[1])
seed = Path('.runtime/offline/kb-methods-fixed6-20261012-r2/seed')
inputs = json.loads(Path('research/kb_cost_20261012/inputs.json').read_bytes())
def load(name):
    return json.loads((seed / name).read_bytes())

lattice = LatticeTimeKB.from_json_obj(load('clause-lattice-time-kb.json'))
lattice.prepare()
trie = ClauseResourceKB.from_json_obj(load('clause-resource-kb.json'))
runtime = RuntimeToolResourceKB.from_json_obj(load('runtime-tool-resource-kb.json'))
edge = EdgeKappaRuntime.from_snapshot(load('edge-kappa-kb.json'))
rows = [ClauseObservation(**{**row, 'argv': tuple(row['argv']),
                            'ts_start': 2e12 + i * 1000, 'ts_end': 2e12 + i * 1000 + 1})
        for i, row in enumerate(inputs['update_clauses'])]
with tempfile.TemporaryDirectory(prefix='clawtune-flush-') as temp:
    state = Path(temp) / 'state'
    initialize_state(state, seed, owner='algorithm-equivalence')
    with StateStore(state) as store:
        predictor = ToolResourcePredictor(
            kb=trie, lattice_kb=lattice, edge_kappa=edge, buckets=LatencyBuckets((100, 500, 2000, 10000)),
            report=None, repo='test', artifact_dir=state,
            clause_kb_snapshot_path=state / 'clause-resource-kb.json',
            runtime_kb_snapshot_path=state / 'runtime-tool-resource-kb.json',
            lattice_kb_snapshot_path=state / 'clause-lattice-time-kb.json')
        predictor.continuous_kb = runtime
        predictor._state_store = store
        predictor._runtime_kb_version = predictor._clause_kb_version = predictor._edge_kappa_version = 1
        start = time.perf_counter()
        predictor._kb_writes.enqueue(rows)
        predictor.flush_kb_updates(timeout_seconds=600)
        elapsed = time.perf_counter() - start
        committed = committed_state(state)
        result = {'flush_seconds': elapsed, 'observations_added': len(rows),
                  'committed': committed, 'pending_writer': predictor.kb_updates_pending(),
                  'scope': 'enqueue + background rebuild + all four snapshots + atomic managed checkpoint; temp files on WSL /tmp'}
        # Commit contains the same observation data and weights, not cache state.
        result['snapshot_sha256'] = hashlib.sha256(json.dumps(
            predictor.lattice_kb.to_json_obj(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        predictor.command_features.close()
        predictor._state_store = None
    (out / 'flush.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
