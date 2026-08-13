from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

from fastapi.testclient import TestClient

from clawtune_sidecar.api.app import create_app
from clawtune_sidecar.api.dependencies import build_state
from clawtune_sidecar.config import SidecarConfig


def _write_cgroup_fixture(path: Path, *, usage_usec: int) -> None:
    path.mkdir()
    (path / "cpu.stat").write_text(f"usage_usec {usage_usec}\n", encoding="utf-8")
    (path / "memory.current").write_text("4096\n", encoding="utf-8")
    (path / "io.stat").write_text("8:0 rbytes=10 wbytes=20\n", encoding="utf-8")
    (path / "cgroup.procs").write_text("", encoding="utf-8")


def _sandbox_scope(cgroup: Path, container_id: str) -> dict[str, object]:
    return {
        "kind": "cgroup-v2",
        "execution_id": None,
        "pid": None,
        "root_pid": None,
        "process_start_time": None,
        "root_starttime_ticks": None,
        "cgroup_path": str(cgroup),
        "pid_namespace_inode": None,
        "container_id": container_id,
        "include_children": True,
        "source": "openclaw-sandbox",
        "attribution_source": "shared-sandbox-container",
    }


def _tool_before(
    *,
    event_id: str,
    run_id: str,
    session_id: str,
    tool_call_id: str,
    marker: str,
    runtime_id: str | None,
    repo: str | None,
    gateway_id: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": "clawtune.v1",
        "event_id": event_id,
        "occurred_at": "2026-08-03T12:00:00Z",
        "plugin_version": "0.1.0",
        "run_id": run_id,
        "session_id": session_id,
        "session_key": None,
        "agent_id": "main",
        "tool_call_id": tool_call_id,
        "tool_name": "read",
        "tool_kind": "file",
        "tool_input_kind": "json",
        "operation_hint": None,
        "derived_paths": [marker],
        "params_digest": "sha256:" + marker[0] * 64,
        "param_features": {
            "serialized_size_bytes": len(marker),
            "string_length": len(marker),
            "list_item_count": 0,
            "path_count": 1,
            "has_command_like_field": False,
        },
        "raw_params": {"path": marker},
        "raw_event": None,
        "resource_scope": None,
    }
    if runtime_id is not None:
        event["runtime_id"] = runtime_id
    if gateway_id is not None:
        event["gateway_id"] = gateway_id
    if repo is not None:
        event["repo"] = repo
    return event


def _tool_completed(
    before: dict[str, object],
    decision: dict[str, object],
    *,
    event_id: str,
) -> dict[str, object]:
    event = {
        key: before[key]
        for key in (
            "schema_version",
            "plugin_version",
            "run_id",
            "session_id",
            "session_key",
            "agent_id",
            "tool_call_id",
        )
    }
    for optional_key in ("gateway_id", "runtime_id", "repo"):
        if optional_key in before:
            event[optional_key] = before[optional_key]
    event.update(
        {
            "event_id": event_id,
            "occurred_at": "2026-08-03T12:00:01Z",
            "decision_id": decision["decision_id"],
            "lease_id": decision["lease_id"],
            "execution_id": None,
            "tool_name": before["tool_name"],
            "duration_ms": 100,
            "succeeded": True,
            "error_type": None,
            "error_digest": None,
            "result_size_bytes": 2,
            "raw_result": "ok",
            "raw_event": None,
            "resource_scope": None,
        }
    )
    return event


def _execution_registration(
    before: dict[str, object],
    *,
    execution_id: str,
) -> dict[str, object]:
    return {
        "execution_id": execution_id,
        "gateway_id": before.get("gateway_id"),
        "runtime_id": before.get("runtime_id"),
        "repo": before.get("repo"),
        "agent_id": before.get("agent_id"),
        "session_id": before.get("session_id"),
        "tool_call_id": before.get("tool_call_id"),
        "run_id": before.get("run_id"),
        "session_key_hash": None,
        "command_digest": "sha256:" + "a" * 64,
        "command": "printf isolated",
        "workdir": "/workspace",
        "host": "gateway",
        "placement": None,
        "profiling": None,
        "backend": "managed-wrapper",
    }


def _records(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _tool_record(
    records: list[dict[str, object]],
    record_type: str,
) -> dict[str, object]:
    return next(
        record
        for record in records
        if record.get("record_type") == record_type and record.get("kind") == "tool"
    )


def test_shared_sidecar_isolates_scopes_and_traces_for_two_runtimes(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            tool_resource_ebpf_required=False,
        )
    )
    cgroup_a = tmp_path / "cgroup-a"
    cgroup_b = tmp_path / "cgroup-b"
    _write_cgroup_fixture(cgroup_a, usage_usec=100_000)
    _write_cgroup_fixture(cgroup_b, usage_usec=500_000)

    before_a = _tool_before(
        event_id="before-a",
        run_id="run-a",
        session_id="session-a",
        tool_call_id="shared-tool-call",
        marker="runtime-a.txt",
        runtime_id="runtime-a",
        repo="owner/repo-a",
    )
    before_b = _tool_before(
        event_id="before-b",
        run_id="run-b",
        session_id="session-b",
        tool_call_id="shared-tool-call",
        marker="runtime-b.txt",
        runtime_id="runtime-b",
        repo="owner/repo-b",
    )

    with TestClient(create_app(state)) as client:
        scope_a = client.post(
            "/v1/runtime/runtime-a/sandbox-scope",
            json=_sandbox_scope(cgroup_a, "container-a"),
        )
        scope_b = client.post(
            "/v1/runtime/runtime-b/sandbox-scope",
            json=_sandbox_scope(cgroup_b, "container-b"),
        )
        assert scope_a.status_code == 200
        assert scope_a.json() == {"stored": True}
        assert scope_b.status_code == 200
        assert scope_b.json() == {"stored": True}

        decision_a_response = client.post("/v1/decisions/tool", json=before_a)
        decision_b_response = client.post("/v1/decisions/tool", json=before_b)
        assert decision_a_response.status_code == 200
        assert decision_b_response.status_code == 200
        decision_a = decision_a_response.json()
        decision_b = decision_b_response.json()

        (cgroup_a / "cpu.stat").write_text("usage_usec 200000\n", encoding="utf-8")
        (cgroup_b / "cpu.stat").write_text("usage_usec 800000\n", encoding="utf-8")

        # Complete in reverse order so a registry keyed only by tool_call_id
        # cannot accidentally appear correct.
        completed_b = client.post(
            "/v1/events/tool-completed",
            json=_tool_completed(before_b, decision_b, event_id="after-b"),
        )
        completed_a = client.post(
            "/v1/events/tool-completed",
            json=_tool_completed(before_a, decision_a, event_id="after-a"),
        )
        assert completed_b.status_code == 200
        assert completed_b.json() == {"stored": True}
        assert completed_a.status_code == 200
        assert completed_a.json() == {"stored": True}

    trace_paths = sorted(trace_dir.glob("*.jsonl"))
    assert len(trace_paths) == 2
    expected_paths = {
        _tool_record(_records(path), "span_start")["runtime_id"]: path
        for path in trace_paths
    }
    assert set(expected_paths) == {"runtime-a", "runtime-b"}

    expected = {
        "runtime-a": ("owner/repo-a", "runtime-a.txt", str(cgroup_a)),
        "runtime-b": ("owner/repo-b", "runtime-b.txt", str(cgroup_b)),
    }
    for runtime_id, path in expected_paths.items():
        records = _records(path)
        span_records = [
            record
            for record in records
            if record.get("record_type") != "trace_metadata"
        ]
        assert span_records
        assert {record.get("runtime_id") for record in span_records} == {runtime_id}
        repo, marker, cgroup_path = expected[runtime_id]
        assert {record.get("repo") for record in span_records} == {repo}

        start = _tool_record(records, "span_start")
        end = _tool_record(records, "span_end")
        assert start["span_id"] == "shared-tool-call"
        assert start["input"] == {"requested_args": {"path": marker}}
        assert start["prediction"]["tool_resource"]["repo"] == repo
        assert end["span_id"] == "shared-tool-call"
        assert end["execution"]["cgroup_path"] == cgroup_path
        assert end["resources"]["scope"] == "cgroup"


def test_runtime_less_events_keep_the_legacy_scope_and_trace_prefix(tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            tool_resource_ebpf_required=False,
        )
    )
    legacy_cgroup = tmp_path / "legacy-cgroup"
    _write_cgroup_fixture(legacy_cgroup, usage_usec=100_000)
    before = _tool_before(
        event_id="legacy-before",
        run_id="legacy-run",
        session_id="legacy-session",
        tool_call_id="legacy-tool-call",
        marker="legacy.txt",
        runtime_id=None,
        repo=None,
    )

    with TestClient(create_app(state)) as client:
        scope_response = client.post(
            "/v1/runtime/sandbox-scope",
            json=_sandbox_scope(legacy_cgroup, "legacy-container"),
        )
        assert scope_response.status_code == 200
        assert scope_response.json() == {"stored": True}

        decision_response = client.post("/v1/decisions/tool", json=before)
        assert decision_response.status_code == 200
        decision = decision_response.json()
        (legacy_cgroup / "cpu.stat").write_text("usage_usec 200000\n", encoding="utf-8")
        completion_response = client.post(
            "/v1/events/tool-completed",
            json=_tool_completed(before, decision, event_id="legacy-after"),
        )
        assert completion_response.status_code == 200
        assert completion_response.json() == {"stored": True}

    trace_paths = list(trace_dir.glob("*.jsonl"))
    assert len(trace_paths) == 1
    trace_path = trace_paths[0]
    records = _records(trace_path)
    span_records = [record for record in records if record.get("record_type") != "trace_metadata"]
    assert span_records
    assert {record.get("runtime_id") for record in span_records} == {None}
    assert {record.get("repo") for record in span_records} == {"openclaw"}
    end = _tool_record(records, "span_end")
    assert end["execution"]["cgroup_path"] == str(legacy_cgroup)
    assert end["resources"]["scope"] == "cgroup"


def test_same_gateway_sessions_cannot_cross_execution_owner(
    tmp_path: Path,
) -> None:
    """A runtime-local execution ID must still belong to one exact Session."""

    state = build_state(
        SidecarConfig(
            trace_dir=tmp_path / "traces",
            tool_resource_ebpf_required=False,
        )
    )
    common = {
        "run_id": "shared-run",
        "tool_call_id": "shared-tool-call",
        "runtime_id": "shared-runtime",
        "repo": "owner/repo",
        "gateway_id": "gateway-0",
    }
    before_a = _tool_before(
        event_id="before-a",
        session_id="session-a",
        marker="a.txt",
        **common,
    )
    before_b = _tool_before(
        event_id="before-b",
        session_id="session-b",
        marker="b.txt",
        **common,
    )
    registration_a = _execution_registration(
        before_a,
        execution_id="shared-execution-id",
    )
    registration_b = _execution_registration(
        before_b,
        execution_id="shared-execution-id",
    )

    with TestClient(create_app(state)) as client:
        registered = client.post("/v2/executions", json=registration_a)
        assert registered.status_code == 200

        conflicting_registration = client.post(
            "/v2/executions",
            json=registration_b,
        )
        assert conflicting_registration.status_code == 409

        foreign_completion = _tool_completed(
            before_b,
            {"decision_id": "decision-b", "lease_id": None},
            event_id="after-b",
        )
        foreign_completion["execution_id"] = "shared-execution-id"
        rejected = client.post(
            "/v1/events/tool-completed",
            json=foreign_completion,
        )
        assert rejected.status_code == 409

    record = state.executions.get("shared-execution-id")
    assert record is not None
    assert record.request.gateway_id == "gateway-0"
    assert record.request.runtime_id == "shared-runtime"
    assert record.request.session_id == "session-a"
    assert record.request.run_id == "shared-run"


def test_shared_sidecar_isolates_128_overlapping_sessions_across_gateways(
    tmp_path: Path,
) -> None:
    """Stress the 8 Gateway x 16 Session topology with adversarial IDs.

    Every tool reuses the same run/tool-call identity and all 128 starts remain
    active together. Reverse-order completion makes any lossy correlation key
    attribute at least one Session to another Session's cgroup or trace.
    """

    trace_dir = tmp_path / "traces"
    state = build_state(
        SidecarConfig(
            trace_dir=trace_dir,
            tool_resource_ebpf_required=False,
        )
    )
    cases: list[dict[str, object]] = []
    for gateway_index in range(8):
        gateway_id = f"gateway-{gateway_index}"
        for session_index in range(16):
            ordinal = gateway_index * 16 + session_index
            # One Gateway runtime intentionally owns all 16 Sessions.  This
            # makes runtime_id, run_id, and tool_call_id insufficient as a
            # correlation key; session identity must participate as well.
            runtime_id = f"runtime-{gateway_index}"
            session_id = f"session-{gateway_index}-{session_index}"
            marker = f"marker-{ordinal:03d}.txt"
            cgroup = tmp_path / f"cgroup-{ordinal:03d}"
            initial_usage = 100_000 + ordinal * 1_000
            _write_cgroup_fixture(cgroup, usage_usec=initial_usage)
            before = _tool_before(
                event_id=f"before-{ordinal:03d}",
                run_id="shared-run",
                session_id=session_id,
                tool_call_id="shared-tool-call",
                marker=marker,
                runtime_id=runtime_id,
                repo=f"owner/repo-{ordinal:03d}",
                gateway_id=gateway_id,
            )
            before["resource_scope"] = _sandbox_scope(
                cgroup,
                f"container-{ordinal:03d}",
            )
            cases.append(
                {
                    "ordinal": ordinal,
                    "gateway_id": gateway_id,
                    "runtime_id": runtime_id,
                    "session_id": session_id,
                    "marker": marker,
                    "cgroup": cgroup,
                    "initial_usage": initial_usage,
                    "before": before,
                }
            )

    with TestClient(create_app(state)) as client:
        def decide(case: dict[str, object]):
            return client.post("/v1/decisions/tool", json=case["before"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            decision_responses = list(executor.map(decide, cases))

        decisions: list[dict[str, object]] = []
        for response in decision_responses:
            assert response.status_code == 200, response.text
            decisions.append(response.json())

        assert state.tool_monitor.active_count() == 128

        for case in cases:
            cgroup = case["cgroup"]
            assert isinstance(cgroup, Path)
            initial_usage = case["initial_usage"]
            assert isinstance(initial_usage, int)
            ordinal = case["ordinal"]
            assert isinstance(ordinal, int)
            (cgroup / "cpu.stat").write_text(
                f"usage_usec {initial_usage + 50_000 + ordinal}\n",
                encoding="utf-8",
            )

        for case, decision in reversed(list(zip(cases, decisions, strict=True))):
            ordinal = case["ordinal"]
            assert isinstance(ordinal, int)
            response = client.post(
                "/v1/events/tool-completed",
                json=_tool_completed(
                    case["before"],
                    decision,
                    event_id=f"after-{ordinal:03d}",
                ),
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"stored": True}

        assert state.tool_monitor.active_count() == 0

    trace_paths = sorted(trace_dir.glob("*.jsonl"))
    assert len(trace_paths) == 128
    expected = {
        (
            str(case["gateway_id"]),
            str(case["runtime_id"]),
            str(case["session_id"]),
        ): case
        for case in cases
    }
    seen: set[tuple[str, str, str]] = set()
    for path in trace_paths:
        records = _records(path)
        start = _tool_record(records, "span_start")
        end = _tool_record(records, "span_end")
        runtime_id = start.get("runtime_id")
        gateway_id = start.get("gateway_id")
        session_id = start.get("session_id")
        assert isinstance(runtime_id, str)
        assert isinstance(gateway_id, str)
        assert isinstance(session_id, str)
        owner = (gateway_id, runtime_id, session_id)
        case = expected[owner]
        seen.add(owner)

        assert start["gateway_id"] == case["gateway_id"]
        assert end["gateway_id"] == case["gateway_id"]
        assert start["session_id"] == case["session_id"]
        assert end["session_id"] == case["session_id"]
        assert start["run_id"] == "shared-run"
        assert end["run_id"] == "shared-run"
        assert start["span_id"] == "shared-tool-call"
        assert end["span_id"] == "shared-tool-call"
        assert start["input"] == {
            "requested_args": {"path": case["marker"]}
        }
        assert end["execution"]["cgroup_path"] == str(case["cgroup"])
        assert end["resources"]["scope"] == "cgroup"

    assert seen == set(expected)
