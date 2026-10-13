"""Compare writer cost and exact learned state using a read-only real corpus.

Run in separate processes with baseline/current sidecar roots on PYTHONPATH.
This measures the shared writer and model queries, not HTTP/collector latency.
Recorded labels/features are unchanged; timestamps are rebased for causal replay.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from clawtune_kb import FILES, StateStore, create_seed, initialize_state
from clawtune_kb import store as storage
from clawtune_sidecar.predictors import tool_resource as owner_module
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
from tool_resource.runtime_kb import (
    ClauseResourceKB,
    LatencyBuckets,
    RuntimeToolResourceKB,
)
from tool_time.lattice_kb import LatticeTimeKB, _observation_from_json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def clause(row):
    return {k: v for k, v in asdict(row).items() if v is not None}


def learned_counts(edge):
    return {'nodes': len(edge.kb.nodes), 'edges': len(edge.kb.edges),
            'edge_updates': sum(e.update_count for e in edge.kb.edges.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--train-count', type=int, default=512)
    parser.add_argument('--batches', type=int, default=8)
    parser.add_argument('--batch-size', type=int, default=4)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    source = json.loads(args.corpus.read_text())
    count = args.train_count + args.batches * args.batch_size
    rows = [_observation_from_json(r) for r in source['observations'][:count]]
    assert len(rows) == count, 'corpus is too small'
    training, incoming = rows[:args.train_count], rows[args.train_count:]
    edges = (100, 500, 2000, 10000)
    started = time.perf_counter()
    lattice = LatticeTimeKB.fit(training, max_optional_features=6)
    lattice.prepare()
    edge = EdgeKappaRuntime.fit(training, edges, max_optional_features=6)
    trie = ClauseResourceKB.fit_public(training)
    trie.merge_historical(training)
    trie._absorb_completed(float('inf'))
    runtime = RuntimeToolResourceKB()
    state = args.out / 'state'
    payloads = dict(zip(FILES, (trie.to_json_obj(), runtime.to_json_obj(), lattice.to_json_obj())))
    payloads['edge-kappa-kb.json'] = edge.to_snapshot()
    create_seed(args.out / 'seed', payloads, provenance={'corpus': str(args.corpus), 'order': 6})
    del payloads
    initialize_state(state, args.out / 'seed', owner='checkpoint-study')
    owner = ToolResourcePredictor(kb=trie, lattice_kb=lattice, edge_kappa=edge,
        buckets=LatencyBuckets(edges), report=None, repo='study', artifact_dir=state,
        clause_kb_snapshot_path=state / FILES[0], runtime_kb_snapshot_path=state / FILES[1],
        lattice_kb_snapshot_path=state / FILES[2])
    owner._state_store = StateStore(state).__enter__()
    initial_edge_counts = learned_counts(edge)
    setup_s = time.perf_counter() - started
    io = {'snapshot_writes': 0, 'snapshot_bytes': 0, 'hash_reads': 0,
          'hash_bytes': 0, 'copies': 0, 'copy_bytes': 0}
    write, digest, copyfile = owner_module._write_json_atomic, storage.digest, storage.shutil.copyfile

    def tracked_write(path, payload):
        write(path, payload)
        io['snapshot_writes'] += 1
        io['snapshot_bytes'] += path.stat().st_size

    def tracked_digest(path):
        io['hash_reads'] += 1
        io['hash_bytes'] += path.stat().st_size
        return digest(path)

    def tracked_copy(src, dst, *a, **kw):
        io['copies'] += 1
        io['copy_bytes'] += Path(src).stat().st_size
        return copyfile(src, dst, *a, **kw)

    owner_module._write_json_atomic = tracked_write
    storage.digest, storage.shutil.copyfile = tracked_digest, tracked_copy
    applied = threading.Event()
    apply_batch = owner._kb_writes._apply_batch
    errors = []

    def apply(batch):
        try:
            apply_batch(batch)
        except BaseException as exc:
            errors.append(repr(exc))
            raise
        finally:
            if batch:
                applied.set()

    owner._kb_writes._apply_batch = apply
    clock = max(r.ts_end for r in training) + 1
    predictions, measurements = [], []
    online_started = time.perf_counter()
    try:
        for b in range(args.batches):
            batch = [replace(r, ts_start=clock + i * .001,
                ts_end=clock + i * .001 + (r.ts_end - r.ts_start))
                for i, r in enumerate(incoming[b * args.batch_size:(b + 1) * args.batch_size])]
            end = max(r.ts_end for r in batch) + 1
            with owner._kb_lock:
                for i, r in enumerate(batch):
                    predictions.append(edge.predict_load_samples(r.repo, [clause(r)], r.ts_start,
                        call_id=f'{b}:{i}'))
                for i in reversed(range(len(batch))):
                    edge.complete_call(f'{b}:{i}', [batch[i]], commit_time=end)
                owner._edge_kappa_version += 1
            applied.clear()
            old_lattice = owner.lattice_kb
            started = time.perf_counter()
            owner._kb_writes.enqueue(batch)
            visible_s = None
            while not applied.wait(.001):
                if visible_s is None and owner.lattice_kb is not old_lattice:
                    visible_s = time.perf_counter() - started
                if time.perf_counter() - started > 180:
                    raise TimeoutError('writer batch did not finish')
            assert not errors, errors
            elapsed = time.perf_counter() - started
            assert owner.lattice_kb is not old_lattice
            with owner._kb_lock:
                for r in batch:
                    predictions.append(owner.lattice_kb.predict_load_samples(r.repo, [clause(r)], end + 1))
            measurements.append({'batch': b, 'visible_s': elapsed if visible_s is None else visible_s,
                                 'worker_batch_s': elapsed})
            clock = end + 2
        online_s = time.perf_counter() - online_started
        before_save = dict(io)
        started = time.perf_counter()
        owner.flush_kb_updates(timeout_seconds=180)
        save_s = time.perf_counter() - started
        after_save = dict(io)
        semantic = {'predictions': predictions, 'lattice': owner.lattice_kb.to_json_obj(),
                    'edge': edge.to_snapshot(), 'trie': trie.to_json_obj(), 'runtime': runtime.to_json_obj()}
        saved_edge = json.loads((state / 'edge-kappa-kb.json').read_text())
        assert fingerprint(saved_edge) == fingerprint(edge.to_snapshot())
        restored = EdgeKappaRuntime.from_snapshot(saved_edge)
        # Loading canonically orders node rows; compare every node/edge value.
        loaded_model, live_model = restored.kb.to_snapshot(), edge.kb.to_snapshot()
        for model in (loaded_model, live_model):
            model['nodes'].sort(key=lambda node: node['id'])
        assert loaded_model == live_model
        del loaded_model, live_model, saved_edge
        saved_lattice = LatticeTimeKB.from_json_obj(json.loads((state / FILES[2]).read_text()))
        # Query-time movement of pending Lattice rows is not a new observation;
        # either snapshot representation must predict the same future query.
        r = batch[-1]
        assert saved_lattice.predict_load_samples(r.repo, [clause(r)], clock) == owner.lattice_kb.predict_load_samples(r.repo, [clause(r)], clock)
        report = {'python': platform.python_version(), 'platform': platform.platform(),
                  'train_count': args.train_count, 'batches': args.batches, 'batch_size': args.batch_size,
                  'max_optional_features': 6, 'corpus_sha256': digest(args.corpus),
                  'setup_s': setup_s, 'online_s': online_s, 'final_save_s': save_s,
                  'online_plus_save_s': online_s + save_s, 'batches_measured': measurements,
                  'online_io': before_save, 'total_io': after_save,
                  'semantic_sha256': fingerprint(semantic), 'restart_predictions_equal': True,
                  'initial_edge_counts': initial_edge_counts, 'final_edge_counts': learned_counts(edge),
                  'peak_rss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}
        (args.out / 'result.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        owner.close(timeout_seconds=180)
        owner_module._write_json_atomic = write
        storage.digest, storage.shutil.copyfile = digest, copyfile


if __name__ == '__main__':
    main()
