"""An acknowledged save includes every queued batch, exactly once."""
import json
import threading

import pytest

from clawtune_sidecar.predictors.tool_resource import KnowledgeBaseFlushError
from test_execution_interruption_regressions import predictor_at
from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB


@pytest.mark.parametrize("stage", ["fork", "second_row", "prepare"])
def test_failed_background_batch_retries_atomically(tmp_path, monkeypatch, stage):
    owner = predictor_at(tmp_path)
    failed = threading.Event()
    original = getattr(LatticeTimeKB, {"fork": "fork_for_update", "second_row": "observe_completed_clause", "prepare": "prepare"}[stage])
    method = {"fork": "fork_for_update", "second_row": "observe_completed_clause", "prepare": "prepare"}[stage]
    calls = []
    def fail_once(self, *args):
        calls.append(1)
        if len(calls) == (2 if stage == "second_row" else 1):
            # Include a failure after mutation, not only before accepting rows.
            if stage != "fork":
                original(self, *args)
            failed.set()
            raise MemoryError("temporary failure")
        return original(self, *args)
    monkeypatch.setattr(LatticeTimeKB, method, fail_once)
    rows = tuple(ClauseObservation("review", "sleep", ("sleep", "1"), i, i + 1, latency_ms=1000)
                 for i in (1, 3))
    try:
        owner._kb_writes.enqueue(rows)
        assert failed.wait(5)
        assert owner.lattice_kb.observation_count == 0
        owner.flush_kb_updates(10)
        # A save preserves pending rows; only a strictly later query absorbs them.
        assert owner.lattice_kb.pending_count == 2
        owner.lattice_kb._advance(10)
        assert owner.lattice_kb.observation_count == 2
        assert owner._lattice_kb_version == 2
        restored = LatticeTimeKB.from_json_obj(json.loads(owner.lattice_kb_snapshot_path.read_text()))
        assert restored.observation_count + restored.pending_count == 2
        owner.flush_kb_updates(10)
        assert owner.lattice_kb.observation_count == 2
    finally:
        owner.close()


def test_persistent_batch_failure_blocks_flush_and_close_then_recovers(tmp_path, monkeypatch):
    owner = predictor_at(tmp_path)
    original = LatticeTimeKB.prepare
    def fail(self):
        raise OSError("cannot prepare")
    monkeypatch.setattr(LatticeTimeKB, "prepare", fail)
    owner._kb_writes.enqueue((ClauseObservation("review", "sleep", ("sleep", "1"), 1, 2, latency_ms=1000),))
    try:
        with pytest.raises(KnowledgeBaseFlushError, match="cannot prepare"):
            owner.flush_kb_updates(5)
        assert owner.kb_updates_pending()
        with pytest.raises(KnowledgeBaseFlushError, match="cannot prepare"):
            owner.close()
        assert not owner._closed
    finally:
        monkeypatch.setattr(LatticeTimeKB, "prepare", original)
        owner.close()
    assert owner.lattice_kb.observation_count + owner.lattice_kb.pending_count == 1
    assert not owner._kb_writes.pending()
