from __future__ import annotations

import concurrent.futures
from pathlib import Path

from tool_resource.runtime_kb import ClauseObservation, ClauseResourceKB, LatencyBuckets
from tool_resource.sdk import (
    CommandObservationToken,
    DockerCommandObserver,
    DockerExecutionContext,
    ToolResourceSDK,
)


class _FakeObserver:
    telemetry_available = True
    unavailable_reason = None

    def __init__(self, context: DockerExecutionContext) -> None:
        self.context = context

    def start(self, tool_call_id: str, command: str) -> CommandObservationToken:
        return CommandObservationToken(tool_call_id, command, object())


def _context(tmp_path: Path, index: int) -> DockerExecutionContext:
    return DockerExecutionContext(
        container_id=f"container-{index}",
        container_executable="docker",
        repo="owner/project",
        artifact_path=tmp_path / f"artifact-{index}.json",
    )


def _sdk() -> ToolResourceSDK:
    kb = ClauseResourceKB.fit_public(
        [
            ClauseObservation(
                repo="public",
                bin="printf",
                argv=("printf", "ok"),
                ts_start=1.0,
                ts_end=1.01,
                latency_ms=10.0,
            )
        ]
    )
    return ToolResourceSDK(kb, LatencyBuckets((100.0, 500.0, 2_000.0)))


def test_sdk_allocates_unique_runs_under_128_concurrent_starts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sdk = _sdk()
    monkeypatch.setattr(
        DockerCommandObserver,
        "attach",
        classmethod(lambda _cls, context: _FakeObserver(context)),
    )

    def start(index: int):
        return sdk.start_command(
            _context(tmp_path, index),
            f"call-{index}",
            "printf ok",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        runs = list(executor.map(start, range(128)))

    assert len({run._run_id for run in runs}) == 128
    assert sdk._pending_run_ids == {run._run_id for run in runs}
    assert len(sdk._pending_artifact_paths) == 128


def test_sdk_rejects_concurrent_reuse_of_an_active_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sdk = _sdk()
    monkeypatch.setattr(
        DockerCommandObserver,
        "attach",
        classmethod(lambda _cls, context: _FakeObserver(context)),
    )
    context = _context(tmp_path, 1)

    sdk.start_command(context, "call-a", "printf a")

    try:
        sdk.start_command(context, "call-b", "printf b")
    except ValueError as exc:
        assert "already exists or is active" in str(exc)
    else:
        raise AssertionError("active artifact path was reused")
