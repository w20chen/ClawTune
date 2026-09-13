#!/usr/bin/env python3
"""Install a matched Docker plugin pair and test real Compose builds."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from urllib.request import urlopen
from uuid import uuid4

VERSIONS = {"compose": "v2.39.4", "buildx": "v0.28.0"}


def docker_config() -> Path:
    return Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker").expanduser().resolve()


def install() -> None:
    arch = platform.machine().lower()
    compose_arch = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}[arch]
    buildx_arch = "arm64" if compose_arch == "aarch64" else "amd64"
    folder = docker_config() / "cli-plugins"
    folder.mkdir(parents=True, exist_ok=True)
    for name, version in VERSIONS.items():
        target = folder / ("docker-" + name)
        if target.is_file():
            result = subprocess.run([str(target), "version"], capture_output=True, text=True)
            if result.returncode == 0 and version in result.stdout.split():
                continue
        asset = f"docker-compose-linux-{compose_arch}" if name == "compose" else f"buildx-{version}.linux-{buildx_arch}"
        base = f"https://github.com/docker/{name}/releases/download/{version}"
        with urlopen(base + "/checksums.txt", timeout=60) as response:
            checksums = response.read().decode()
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1].lstrip("*") == asset)
        # Keep the existing executable intact until download and checksum succeed.
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            download = Path(temp) / asset
            with urlopen(base + "/" + asset, timeout=120) as response, download.open("wb") as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(download.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"{name} checksum mismatch")
            download.chmod(0o755)
            if target.exists() or target.is_symlink():
                target.rename(folder / (target.name + ".backup-" + uuid4().hex[:8]))
            download.replace(target)
        print(f"Installed Docker {name} {version}", flush=True)


def check() -> None:
    env = dict(os.environ, DOCKER_CONFIG=str(docker_config()))
    def run(*args, **kwargs):
        return subprocess.run(["docker", *args], env=env, check=True, timeout=180, **kwargs)
    run("info", "--format", "{{.ServerVersion}}")
    run("compose", "version")
    run("buildx", "version")
    name = "clawtune-setup-" + uuid4().hex[:12]
    # No base-image download: this tests Compose -> Buildx -> daemon end to end.
    with tempfile.TemporaryDirectory(prefix="clawtune-docker-check-") as temp:
        folder = Path(temp)
        (folder / "marker").write_text("clawtune-setup-ok\n")
        (folder / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
        compose = folder / "compose.yaml"
        compose.write_text(f"services:\n  check:\n    image: {name}:latest\n    build: .\n")
        try:
            run("compose", "-p", name, "-f", str(compose), "build", "--builder", "default")
            run("image", "inspect", name + ":latest", "--format", "{{.Id}}")
        finally:
            subprocess.run(["docker", "image", "rm", name + ":latest"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    print("Docker Compose/Buildx build check passed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "check"])
    args = parser.parse_args()
    install() if args.command == "install" else check()
