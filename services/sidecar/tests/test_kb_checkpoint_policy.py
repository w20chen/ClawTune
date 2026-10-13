"""Online learning is independent of the explicit disk-save boundary."""
import json
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_kb_incremental_equivalence import row
from test_tool_resource_predictor import _tool_request

from clawtune_kb import FILES, StateStore, create_seed, initialize_state
from clawtune_kb import store as storage
from clawtune_sidecar.predictors import tool_resource as owner_module
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
from tool_resource.runtime_kb import ClauseResourceKB, LatencyBuckets, RuntimeToolResourceKB
from tool_time.lattice_kb import LatticeTimeKB


def test_online_batches_and_predictions_do_not_capture_or_write_until_flush(tmp_path, monkeypatch):
    owner = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
        artifact_dir=tmp_path, buckets=LatencyBuckets((100, 500, 2000)), repo='r')
    reference = LatticeTimeKB(max_optional_features=6)
    applied = threading.Event()
    original_apply = owner._kb_writes._apply_batch

    def apply(rows):
        original_apply(rows)
        if rows:
            applied.set()

    monkeypatch.setattr(owner._kb_writes, '_apply_batch', apply)
    captures, writes = [], []
    original_capture = owner._capture_kb_snapshot_locked
    original_write = owner_module._write_json_atomic
    monkeypatch.setattr(owner, '_capture_kb_snapshot_locked',
        lambda *a, **k: (captures.append(k['name']), original_capture(*a, **k))[1])
    monkeypatch.setattr(owner_module, '_write_json_atomic',
        lambda path, obj: (writes.append(path.name), original_write(path, obj))[1])
    try:
        # Query lifecycle state must also wait for an explicit save.
        request = _tool_request('query', 'query', 'python run.py')
        owner.predict(request)
        owner.finalize_call(request)
        for i in range(3):
            observation = row(2_000_000_000 + i, ['a', str(i)])
            applied.clear()
            owner._kb_writes.enqueue((observation,))
            assert applied.wait(5), 'online update incorrectly waits for a save'
            reference.observe_completed_clause(observation)
            reference.prepare()
            ts = observation.ts_end + .25
            clauses = [{k: v for k, v in asdict(observation).items() if v is not None}]
            with owner._kb_lock:
                assert owner.lattice_kb.predict_load_samples('r', clauses, ts) == reference.predict_load_samples('r', clauses, ts)
                assert owner.lattice_kb.observation_count == i + 1
            assert captures == writes == []
            assert not list(tmp_path.glob('*.json'))
        owner.flush_kb_updates(5)
        assert 'lattice' in captures and 'edge_kappa' in captures
        restored = LatticeTimeKB.from_json_obj(json.loads(owner.lattice_kb_snapshot_path.read_text()))
        assert restored.to_json_obj() == owner.lattice_kb.to_json_obj()
        count = len(writes)
        owner.flush_kb_updates(5)
        assert len(writes) == count
    finally:
        owner.close()


def test_normal_close_waits_for_save_after_thirty_seconds(tmp_path, monkeypatch):
    owner = ToolResourcePredictor.from_traces(openclaw_trace_paths=(), ebpf_trace_paths=(),
        artifact_dir=tmp_path, buckets=LatencyBuckets((100, 500, 2000)), repo='r')
    offset = 0
    monkeypatch.setattr(owner_module, 'time', SimpleNamespace(monotonic=lambda: time.monotonic() + offset))

    def finish_parser(**kwargs):
        nonlocal offset
        offset += 31  # Simulate elapsed shutdown time without a 31-second test.

    monkeypatch.setattr(owner.command_features, 'close', finish_parser)
    saving, release, closed = threading.Event(), threading.Event(), threading.Event()
    persist = owner._kb_writes._persist
    errors = []

    def save():
        saving.set()
        assert release.wait(5)
        persist()

    def close():
        try:
            owner.close()
        except BaseException as exc:  # noqa: BLE001 - surface failures from the closing thread
            errors.append(exc)
        finally:
            closed.set()

    monkeypatch.setattr(owner._kb_writes, '_persist', save)
    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert saving.wait(3)
        assert not closed.wait(.05), 'normal shutdown abandoned the final save'
    finally:
        release.set()
        thread.join(5)
    assert closed.is_set() and not errors
    assert owner.edge_kappa_snapshot_path.is_file()


def test_edge_save_frequency_preserves_overlapping_feedback_and_restart(tmp_path):
    rows = [row(0, ['a']), row(1, ['a', 'b'])]
    expected = EdgeKappaRuntime.fit(rows, (100, 500, 2000), max_optional_features=6)
    actual = EdgeKappaRuntime.from_snapshot(expected.to_snapshot())
    owner = ToolResourcePredictor(kb=ClauseResourceKB(), edge_kappa=actual,
        buckets=LatencyBuckets((100, 500, 2000)), report=None, repo='r', artifact_dir=tmp_path)
    try:
        for base in (10, 20, 30):
            # Overlap two calls, and complete them in the opposite order.
            batch = [row(base + i, ['a', 'b'] if i else ['a']) for i in range(2)]
            for observation in batch:
                args = ('r', [asdict(observation)], observation.ts_start)
                key = str(observation.ts_start)
                assert actual.predict_load_samples(*args, call_id=key) == expected.predict_load_samples(*args, call_id=key)
            for observation in reversed(batch):
                for runtime in (actual, expected):
                    runtime.complete_call(str(observation.ts_start), [observation], commit_time=base + 3)
            owner._edge_kappa_version += 1
            # Reference takes a complete snapshot every batch; actual saves once.
            assert expected.capture_snapshot()() == expected.to_snapshot()
            assert actual.to_snapshot() == expected.to_snapshot()
        assert any(edge.update_count for edge in actual.kb.edges.values())
        assert not owner.edge_kappa_snapshot_path.exists()
        owner.flush_kb_updates(5)
        saved = json.loads(owner.edge_kappa_snapshot_path.read_text())
        assert saved == json.loads(json.dumps(expected.to_snapshot()))
        restored = EdgeKappaRuntime.from_snapshot(saved)
        expected = EdgeKappaRuntime.from_snapshot(json.loads(json.dumps(expected.to_snapshot())))
        assert restored.to_snapshot() == expected.to_snapshot()
        observation = row(40, ['a', 'b', 'new'])
        args = ('r', [asdict(observation)], 40)
        assert restored.predict_load_samples(*args, call_id='40') == expected.predict_load_samples(*args, call_id='40')
        for runtime in (restored, expected):
            runtime.complete_call('40', [observation], commit_time=42)
        assert restored.to_snapshot() == expected.to_snapshot()
    finally:
        owner.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_managed_checkpoints_restore_without_snapshot_hashes(tmp_path, monkeypatch, legacy):
    seed, path = tmp_path / 'seed', tmp_path / 'state'
    payloads = dict(zip(FILES, (ClauseResourceKB().to_json_obj(),
        RuntimeToolResourceKB().to_json_obj(), LatticeTimeKB().to_json_obj())))
    create_seed(seed, payloads, provenance={'test': True})
    initialize_state(path, seed, owner='test')
    if legacy:
        current = path / 'generations' / (path / 'CURRENT').read_text()
        old = json.loads((current / 'state.json').read_text())
        old.update(schema='clawtune.kb-state.v1', snapshots={name: storage.digest(current / name) for name in FILES})
        storage.write_json(current / 'state.json', old)
    monkeypatch.setattr(storage, 'digest', lambda *_: pytest.fail('writable state must not hash snapshots'))
    with StateStore(path) as store:
        before = storage.committed_state(path)['generation']
        assert store.checkpoint()['generation'] == before
        kb = ClauseResourceKB()
        kb.observe_completed_clause(row(10, ['a']))
        updated = kb.to_json_obj()
        storage.write_json(path / FILES[0], updated)
        state = store.checkpoint()
        assert state['schema'] == 'clawtune.kb-state.v2'
        assert state['generation'] == before + 1
        assert store.checkpoint() == state
    (path / FILES[0]).write_text('interrupted uncommitted write')
    with StateStore(path):
        assert json.loads((path / FILES[0]).read_text()) == json.loads(json.dumps(updated))
        assert storage.committed_state(path) == state


@pytest.mark.parametrize('filename', FILES)
@pytest.mark.parametrize('broken', ['{"truncated":', '{"schema": "invalid"}'])
@pytest.mark.parametrize('legacy', [False, True])
def test_corrupt_managed_snapshot_refuses_startup_without_overwriting_history(
    tmp_path, filename, broken, legacy,
):
    seed, path = tmp_path / 'seed', tmp_path / 'state'
    payloads = dict(zip(FILES, (ClauseResourceKB().to_json_obj(),
        RuntimeToolResourceKB().to_json_obj(), LatticeTimeKB().to_json_obj())))
    create_seed(seed, payloads, provenance={'test': True})
    initialize_state(path, seed, owner='test')
    current = (path / 'CURRENT').read_bytes()
    folder = path / 'generations' / current.decode().strip()
    if legacy:
        state = json.loads((folder / 'state.json').read_text())
        state.update(schema='clawtune.kb-state.v1',
                     snapshots={name: storage.digest(folder / name) for name in FILES})
        storage.write_json(folder / 'state.json', state)
    snapshot = folder / filename
    original = snapshot.read_bytes()
    snapshot.write_text(broken)
    committed = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    kwargs = {'openclaw_trace_paths': (), 'ebpf_trace_paths': (), 'artifact_dir': path,
              'buckets': LatencyBuckets((100, 500, 2000))}
    with pytest.raises(ValueError, match='managed .* KB snapshot rejected'):
        ToolResourcePredictor.from_traces(**kwargs)
    assert (path / 'CURRENT').read_bytes() == current
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == committed
    # Failed loading releases ownership and never saves an empty replacement.
    snapshot.write_bytes(original)
    restored = ToolResourcePredictor.from_traces(**kwargs)
    restored.close()
