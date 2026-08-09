from __future__ import annotations

import sys
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


def test_repo_root_returns_none_without_checkout(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    package_file = outside / "site-packages" / "agent_scheduler" / "__init__.py"
    monkeypatch.chdir(outside)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: outside))
    monkeypatch.setitem(
        sys.modules,
        "agent_scheduler",
        SimpleNamespace(__file__=str(package_file)),
    )

    assert cli._repo_root() is None
