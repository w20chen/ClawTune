from __future__ import annotations
import importlib.util
import io
from pathlib import Path
import subprocess
import hashlib

import pytest

SPEC = importlib.util.spec_from_file_location("docker_tools", Path(__file__).resolve().parents[1] / "scripts/setup/docker_tools.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_failed_checksum_preserves_existing_plugin(tmp_path, monkeypatch):
    target = tmp_path / "cli-plugins" / "docker-compose"
    target.parent.mkdir()
    target.write_bytes(b"existing plugin")
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "v1.0", ""))
    monkeypatch.setattr(module, "urlopen", lambda url, **kw: io.BytesIO(
        ("0"*64 + "  docker-compose-linux-aarch64\n").encode() if url.endswith("checksums.txt") else b"corrupted"))
    with pytest.raises(RuntimeError, match="checksum"):
        module.install()
    assert target.read_bytes() == b"existing plugin"


def test_installs_both_plugins_in_same_selected_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    downloaded = []
    def download(url, **kw):
        if url.endswith("checksums.txt"):
            asset = "docker-compose-linux-aarch64" if "/compose/" in url else "buildx-v0.28.0.linux-arm64"
            return io.BytesIO((hashlib.sha256(asset.encode()).hexdigest()+"  "+asset+"\n").encode())
        asset = url.rsplit("/",1)[1]
        downloaded.append(asset)
        return io.BytesIO(asset.encode())
    monkeypatch.setattr(module, "urlopen", download)
    module.install()
    assert set(downloaded) == {"docker-compose-linux-aarch64", "buildx-v0.28.0.linux-arm64"}
    assert (tmp_path / "cli-plugins/docker-compose").is_file()
    assert (tmp_path / "cli-plugins/docker-buildx").is_file()
