import importlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig
from clawtune_sidecar.predictors.tool_resource import ExecutionTelemetrySummary
from test_execution_interruption_regressions import predictor_at
from test_runtime_abort import register
from test_tool_resource_predictor import _runtime_sample, _tool_completion
from tool_resource.runtime_kb import ClauseObservation


def test_late_completion_without_before_hook_preserves_real_memory(tmp_path, monkeypatch):
    state = build_state(SidecarConfig(trace_dir=tmp_path / 'traces', tool_resource_ebpf_enabled=False))
    state.predictor.close()
    p = state.predictor = predictor_at(tmp_path)
    monkeypatch.setattr(importlib.import_module('clawtune_sidecar.api.app'), '_EBPF_COMPLETION_GRACE_SECONDS', .01)
    row = ClauseObservation('review', 'sleep', ('sleep', '1'), 1., 2., latency_ms=1000)
    call = {'command': 'sleep 1', 'eligible_for_kb': True, 'clauses': [
        {'bin': 'sleep', 'argv': ['sleep', '1'], 'ts_start': 1., 'ts_end': 2., 'latency_ms': 1000}]}
    artifact = {'version': 2, 'mode': 'clause', 'calls': [call]}
    def finish(run, *args, **kwargs):
        run._observer.context.artifact_path.write_text(json.dumps(artifact))
        return SimpleNamespace(kb_observations_added=1, kb_observations=(row,),
            telemetry_artifact=artifact, call_telemetry=call, kb_update_error=None)
    monkeypatch.setattr(p._sdk, 'start_command', lambda context, call_id, command:
        SimpleNamespace(tool_call_id=call_id, _observer=SimpleNamespace(context=context)))
    monkeypatch.setattr(p._sdk, 'finish_command', finish)
    sample = replace(_runtime_sample('done', 'call-x'), environment_memory={
        'memory_total_peak_bytes': 4096, 'memory_extra_peak_bytes': 2048})
    monkeypatch.setattr(state.tool_monitor, 'complete', lambda *a, **k: sample)
    with TestClient(create_app(state)) as client:
        token = register(client, 'x', runtime='r', gateway='g')
        p.begin_execution(execution_id='x', tool_call_id='call-x', container_id='c',
            command='sleep 1', gateway_id='g', runtime_id='r')
        assert client.post('/v2/executions/x/exited', json={
            'update_token': token, 'exit_code': 0, 'signal': None}).status_code == 200
        deadline = time.monotonic() + 5
        while 'x' in state._ebpf_finalize_tasks or p.execution_active('x'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        event = _tool_completion('done', 'call-x').model_copy(update={
            'execution_id': 'x', 'gateway_id': 'g', 'runtime_id': 'r', 'run_id': 'run',
            'raw_event': {'params': {'command': 'sleep 1'}}})
        response = client.post('/v1/events/tool-completed', json=event.model_dump(mode='json'))
        assert response.status_code == 200, response.text
        snapshot = p.kb.to_json_obj()
        memory = [r['memory_total_peak_bytes'] for r in snapshot['pending']
            if r.get('memory_total_peak_bytes') is not None]
        assert memory == [4096], memory


def test_partial_memory_failure_must_not_add_zero_for_already_learned_clause(tmp_path, monkeypatch):
    p = predictor_at(tmp_path)
    rows = tuple(ClauseObservation('review', 'sleep', ('sleep', str(i)), i, i + 1,
        latency_ms=1000) for i in (1, 3))
    clauses = [{'bin': r.bin, 'argv': list(r.argv), 'ts_start': r.ts_start, 'ts_end': r.ts_end,
        'memory_total_peak_bytes': 4096, 'memory_extra_peak_bytes': 2048} for r in rows]
    p._pending_memory_targets['x'] = (('g', 'r'), rows)
    p._execution_owners['x'] = ('g', 'r')
    p._telemetry_by_execution_id['x'] = ExecutionTelemetrySummary('x', 'call', None, True, 'ok', replay_execution='completed')
    p._completion_inputs['x'] = (('g', 'r'), {'calls': [{'command': 'sleep 1; sleep 3', 'clauses': clauses}]})
    original = p.kb.observe_completed_clause
    calls = []
    def fail_second(row):
        calls.append(row)
        if len(calls) == 2:
            raise OSError('injected second-clause failure')
        return original(row)
    monkeypatch.setattr(p.kb, 'observe_completed_clause', fail_second)
    event = _tool_completion('done', 'call').model_copy(update={
        'execution_id': 'x', 'raw_event': {'params': {'command': 'sleep 1; sleep 3'}}})
    try:
        p.observe_completion(event, _runtime_sample('done', 'call'))
        monkeypatch.setattr(p.kb, 'observe_completed_clause', original)
        p.finalize_runtime('r', 'g')
        snapshot = p.kb.to_json_obj()
        memory = [r['memory_total_peak_bytes'] for r in snapshot['pending']
            if r.get('ts_start') == 1 and r.get('memory_total_peak_bytes') is not None]
        assert memory == [4096], memory
    finally:
        p.close()


def test_trace_shutdown_failure_still_flushes_and_closes_predictor(tmp_path):
    state = build_state(SidecarConfig(trace_dir=tmp_path / 'traces', tool_resource_ebpf_enabled=False))
    state.predictor.close()
    p = state.predictor = predictor_at(tmp_path)
    p._kb_writes.enqueue((ClauseObservation('review', 'sleep', ('sleep', '1'),
        1., 2., latency_ms=1000),))
    try:
        with pytest.raises(RuntimeError, match='task trace persistence failed'):
            with TestClient(create_app(state)):
                # This is the sticky error set by the real writer on disk failure.
                state.trace_writer._write_error = OSError('trace disk unavailable')
        assert p._closed, 'trace close exception skipped predictor.close and its KB flush'
        assert p.lattice_kb_snapshot_path.is_file()
    finally:
        p.close()
