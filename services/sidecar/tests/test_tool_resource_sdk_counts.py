from pathlib import Path
from types import SimpleNamespace

import pytest

from tool_resource import sdk as sdk_module
from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, LatencyBuckets
from tool_resource.sdk import ToolResourceSDK


@pytest.mark.parametrize("mode", ["normal", "frozen", "partial_failure"])
def test_finish_command_counts_only_accepted_observations(tmp_path: Path, monkeypatch, mode):
    kb = ClauseResourceKB()
    sdk = ToolResourceSDK(kb, LatencyBuckets((100.0, 500.0)))
    observations = tuple(
        ClauseObservation(
            repo="demo", bin=bin_, argv=(bin_,), ts_start=1.0, ts_end=2.0,
            latency_ms=1000.0, in_pipe=True, pipeline_position=position,
        )
        for position, bin_ in enumerate(("cat", "head"))
    )
    if mode == "frozen":
        kb.freeze()
    elif mode == "partial_failure":
        original = kb.observe_completed_clause

        def observe(observation):
            if observation.bin == "head":
                raise ValueError("injected failure")
            return original(observation)

        monkeypatch.setattr(kb, "observe_completed_clause", observe)
    call = {"tool_call_id": "call", "command": "cat | head", "eligible_for_kb": True}
    artifact = {"calls": [call]}
    path = tmp_path / "artifact.json"
    observer = SimpleNamespace(
        context=SimpleNamespace(artifact_path=path, container_id="container", repo="demo"),
        finish=lambda *args, **kwargs: call,
        finalize=lambda **kwargs: None,
    )
    run = SimpleNamespace(
        _owner=sdk._owner, _run_id=1, _observer=observer,
        _observation_token=None, tool_call_id="call", command="cat | head",
    )
    sdk._pending_run_ids.add(1)
    sdk._pending_artifact_paths.add(path.resolve())
    monkeypatch.setattr(sdk_module, "_read_artifact", lambda *args: artifact)
    monkeypatch.setattr(sdk_module, "_validate_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(sdk_module, "_observations_from_call", lambda *args, **kwargs: observations)
    result = sdk.finish_command(run, {})
    expected = () if mode == "frozen" else (observations[0],)
    assert result.kb_observations == expected
    assert result.kb_observations_added == len(expected) == len(kb._pending)
    assert result.kb_update_error == (
        "ValueError: injected failure" if mode == "partial_failure" else None
    )
    assert not sdk._pending_run_ids
    assert not sdk._pending_artifact_paths
