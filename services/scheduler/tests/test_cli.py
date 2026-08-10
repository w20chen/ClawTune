from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_scheduler import cli


def test_repo_root_follows_editable_package_path(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "checkout"
    marker = repo / "scripts" / "clawtune.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")
    package_file = (
        repo
        / "services"
        / "scheduler"
        / "src"
        / "agent_scheduler"
        / "__init__.py"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: outside))
    monkeypatch.setitem(
        sys.modules,
        "agent_scheduler",
        SimpleNamespace(__file__=str(package_file)),
    )

    assert cli._repo_root() == repo.resolve()


def test_repo_root_returns_none_without_checkout(monkeypatch) -> None:
    # Use the OS temp dir (not the pytest tmp_path fixture): the project pins
    # --basetemp to ../../.pytest-tmp, which lives *inside* the real checkout.
    # A fake package path under that dir would walk up to the real repo root
    # and find scripts/clawtune.py, making _repo_root() a false positive.
    outside = Path(tempfile.mkdtemp(prefix="clawtune-no-checkout-"))
    try:
        package_file = outside / "site-packages" / "agent_scheduler" / "__init__.py"
        monkeypatch.chdir(outside)
        monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: outside))
        monkeypatch.setitem(
            sys.modules,
            "agent_scheduler",
            SimpleNamespace(__file__=str(package_file)),
        )

        assert cli._repo_root() is None
    finally:
        shutil.rmtree(outside, ignore_errors=True)
