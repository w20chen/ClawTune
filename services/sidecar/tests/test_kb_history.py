import json

import pytest

from tool_resource.runtime_kb import (
    ClauseObservation, ClauseResourceKB, CompletedCall, RuntimeToolResourceKB, ToolCallQuery,
)


def call(start=0, duration=1):
    return CompletedCall("repo", "read", None, start, start + duration)


def clause(start=0, duration=1):
    return ClauseObservation("repo", "python", ("python", "job.py"), start, start + duration,
                             latency_ms=duration * 1000, cpu_ns_cumulative=1_000_000_000)


@pytest.mark.parametrize("kind", ["runtime", "trie"])
@pytest.mark.parametrize("legacy", [False, True])
def test_history_survives_pending_absorption_restart_and_new_identical_measurements(kind, legacy):
    cls, row = (RuntimeToolResourceKB, call) if kind == "runtime" else (ClauseResourceKB, clause)
    kb = cls()
    observe = lambda kb, item: (kb.observe_completed_call(item) if kind == "runtime"
                               else kb.observe_completed_clause(item))
    def values(kb, ts):
        if kind == "runtime":
            return kb.predict_load_samples(ToolCallQuery("repo", "read", None, ts))["duration_ms"]["values"]
        return kb.predict_load_samples("repo", [{"bin": "python", "argv": ["python", "job.py"]}], ts)[0]["duration_ms"]["values"]

    observe(kb, row())
    assert values(kb, 2) == (1000,)
    observe(kb, row(3))  # same measurement, distinct execution, still pending
    snapshot = kb.to_json_obj()
    if legacy:
        snapshot.pop("observed_counts")
        snapshot.pop("legacy_counts")
    kb = cls.from_json_obj(json.loads(json.dumps(snapshot)))
    assert kb.merge_historical([row(), row(3)]) == 0
    assert values(kb, 3) == (1000,)  # replay does not expose future pending data
    assert values(kb, 5) == (1000, 1000)
    for _ in range(3):
        kb = cls.from_json_obj(json.loads(json.dumps(kb.to_json_obj())))
        assert kb.merge_historical([row(), row(3)]) == 0
        assert values(kb, 5) == (1000, 1000)
    assert kb.merge_historical([row(), row(3), row(6)]) == 1
    assert values(kb, 8) == (1000, 1000, 1000)
    observe(kb, row(9))  # online learning must not be deduplicated by value
    assert values(kb, 11) == (1000, 1000, 1000, 1000)
    kb.freeze()
    before = kb.to_json_obj()
    assert kb.merge_historical([row(12)]) == 0
    assert kb.to_json_obj() == before


@pytest.mark.parametrize("cls,row", [(RuntimeToolResourceKB, call), (ClauseResourceKB, clause)])
def test_history_preserves_multiplicity_and_public_priors(cls, row):
    kb = cls.fit_public([row()])
    assert kb.merge_historical([row(), row()]) == 2
    kb = cls.from_json_obj(json.loads(json.dumps(kb.to_json_obj())))
    assert kb.merge_historical([row(), row()]) == 0
    assert kb.merge_historical([row(), row(), row()]) == 1

