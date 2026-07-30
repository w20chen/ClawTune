from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tool_resource import mvdan_client


class _HandshakeClient:
    failures_remaining = 0
    entered_paths: list[Path] = []

    def __init__(self, binary_path: Path) -> None:
        self.binary_path = binary_path

    def __enter__(self) -> "_HandshakeClient":
        type(self).entered_paths.append(self.binary_path)
        if type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise mvdan_client.MvdanClientError("stale or missing adapter")
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_ensure_compatible_adapter_reuses_successful_handshake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "adapter"
    _HandshakeClient.failures_remaining = 0
    _HandshakeClient.entered_paths = []
    monkeypatch.setattr(mvdan_client, "MvdanClient", _HandshakeClient)
    monkeypatch.setattr(mvdan_client, "default_binary_path", lambda: binary)

    def unexpected_build(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("compatible adapter must not be rebuilt")

    monkeypatch.setattr(mvdan_client.subprocess, "run", unexpected_build)

    assert mvdan_client.ensure_compatible_adapter() == binary
    assert _HandshakeClient.entered_paths == [binary]


def test_ensure_compatible_adapter_runs_non_executable_builder_via_sh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "adapter"
    calls: list[tuple[list[str], Path, bool]] = []
    _HandshakeClient.failures_remaining = 1
    _HandshakeClient.entered_paths = []
    monkeypatch.setattr(mvdan_client, "MvdanClient", _HandshakeClient)
    monkeypatch.setattr(mvdan_client, "default_binary_path", lambda: binary)

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> None:
        calls.append((argv, cwd, check))

    monkeypatch.setattr(mvdan_client.subprocess, "run", fake_run)

    assert mvdan_client.ensure_compatible_adapter() == binary
    assert calls == [
        (
            ["/bin/sh", str(mvdan_client._BUILD_SCRIPT)],
            mvdan_client._BUILD_SCRIPT.parent,
            True,
        )
    ]
    assert _HandshakeClient.entered_paths == [binary, binary]


def test_ensure_compatible_adapter_reports_builder_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "adapter"
    _HandshakeClient.failures_remaining = 1
    _HandshakeClient.entered_paths = []
    monkeypatch.setattr(mvdan_client, "MvdanClient", _HandshakeClient)
    monkeypatch.setattr(mvdan_client, "default_binary_path", lambda: binary)

    def failed_build(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(7, ["/bin/sh", "build.sh"])

    monkeypatch.setattr(mvdan_client.subprocess, "run", failed_build)

    with pytest.raises(
        mvdan_client.MvdanClientError,
        match="failed to build compatible mvdan adapter",
    ):
        mvdan_client.ensure_compatible_adapter()


def test_mvdan_versions_are_consistent_across_client_builder_and_go_module() -> None:
    adapter_root = mvdan_client._BUILD_SCRIPT.parent
    build_script = mvdan_client._BUILD_SCRIPT.read_text(encoding="utf-8")
    go_module = (adapter_root / "go.mod").read_text(encoding="utf-8")

    parser_match = re.search(r"^parser_version=(\S+)$", build_script, re.MULTILINE)
    protocol_match = re.search(
        r"^adapter_protocol_version=(\d+)$",
        build_script,
        re.MULTILINE,
    )
    go_toolchain_match = re.search(
        r"^go_version=(\S+)$",
        build_script,
        re.MULTILINE,
    )

    assert parser_match is not None
    assert protocol_match is not None
    assert go_toolchain_match is not None
    assert parser_match.group(1) == mvdan_client.PARSER_VERSION
    assert int(protocol_match.group(1)) == mvdan_client.ADAPTER_PROTOCOL_VERSION
    assert f"require mvdan.cc/sh/v3 {mvdan_client.PARSER_VERSION}" in go_module
    assert f"toolchain go{go_toolchain_match.group(1)}" in go_module
    assert (
        f"protocol-{mvdan_client.ADAPTER_PROTOCOL_VERSION}"
        in mvdan_client.default_binary_path().name
    )
    assert f"mvdan-{mvdan_client.PARSER_VERSION}" in (
        mvdan_client.default_binary_path().name
    )
