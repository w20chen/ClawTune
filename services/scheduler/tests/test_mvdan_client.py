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


def test_default_client_provisions_for_the_runtime_identity(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "adapter"
    created: list[Path] = []

    class FakeClient:
        def __init__(self, binary_path: Path) -> None:
            created.append(binary_path)

    monkeypatch.setattr(mvdan_client, "ensure_compatible_adapter", lambda: binary)
    monkeypatch.setattr(mvdan_client, "MvdanClient", FakeClient)
    monkeypatch.setattr(mvdan_client, "_default_client", None)

    client = mvdan_client.get_client()

    assert isinstance(client, FakeClient)
    assert created == [binary]


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
    assert "-linux-$go_arch" in build_script
    assert (
        "031f088e5d955bab8657ede27ad4e3bc5b7c1ba281f05f245bcc304f327c987a"
        in build_script
    )
    assert (
        "a290581cfe4fe28ddd737dde3095f3dbeb7f2e4065cab4eae44dfc53b760c2f7"
        in build_script
    )
    assert ".tar.gz.sha256" not in build_script
    assert (
        f"protocol-{mvdan_client.ADAPTER_PROTOCOL_VERSION}"
        in mvdan_client.default_binary_path().name
    )
    assert f"mvdan-{mvdan_client.PARSER_VERSION}" in (
        mvdan_client.default_binary_path().name
    )
    assert mvdan_client.default_binary_path().name.endswith(
        mvdan_client._cache_platform_tag()
    )


def test_mvdan_cache_path_is_architecture_specific(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(mvdan_client.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mvdan_client.platform, "machine", lambda: "aarch64")
    arm_path = mvdan_client.default_binary_path()

    monkeypatch.setattr(mvdan_client.platform, "machine", lambda: "x86_64")
    x86_path = mvdan_client.default_binary_path()

    assert arm_path.name.endswith("linux-arm64")
    assert x86_path.name.endswith("linux-amd64")
    assert arm_path != x86_path
