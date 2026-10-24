"""Source releases must carry runtime data without a parent checkout."""
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def test_installed_contract_data_does_not_depend_on_parent_depth(monkeypatch):
    import clawtune_kb.contracts as contracts
    import clawtune_sidecar.config as config

    # A valid shallow --target install must use its bundled data, even if an
    # unrelated ancestor happens to contain a checkout's contracts directory.
    installed = Path(Path.cwd().anchor) / "site" / "clawtune_kb" / "contracts.py"
    monkeypatch.setattr(contracts, "__file__", str(installed))
    assert contracts.data_root() == installed.parent / "_data"
    monkeypatch.setattr(config, "__file__", str(installed.parent.parent / "clawtune_sidecar/config.py"))
    assert config._repo_root() == Path.cwd()


def test_installed_contract_data_ignores_unrelated_parent_checkout(tmp_path, monkeypatch):
    import clawtune_kb.contracts as contracts

    unrelated = tmp_path / "ancestor"
    schema = unrelated / "contracts/kb-seed.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}", encoding="utf-8")
    installed = unrelated / "a/b/c/clawtune_kb/contracts.py"
    monkeypatch.setattr(contracts, "__file__", str(installed))
    assert contracts.data_root() == installed.parent / "_data"


def test_source_release_includes_seed_and_public_contracts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / "repo"
    package = fixture / "services/sidecar"
    module = package / "src/clawtune_kb"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(root / "services/sidecar/setup.py", package / "setup.py")
    shutil.copytree(root / "contracts", fixture / "contracts")
    shutil.copytree(root / "seeds/bootstrap-v2", fixture / "seeds/bootstrap-v2")
    shutil.copytree(root / "seeds/clawbox-qualified-v1", fixture / "seeds/clawbox-qualified-v1")
    output = tmp_path / "dist"
    result = subprocess.run([sys.executable, "setup.py", "sdist", "--dist-dir", str(output)],
                            cwd=package, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        files = [member for member in archive.getmembers() if member.isfile()]
        seeds = [member for member in files if "/_data/seeds/" in member.name]
        assert len(seeds) == 10
        assert any(member.name.endswith("/edge-kappa-kb.json") for member in seeds)
        for member in seeds:
            relative = member.name.split("/_data/", 1)[1]
            assert Path(relative).parts[1] in {"bootstrap-v2", "clawbox-qualified-v1"}
            assert archive.extractfile(member).read() == (root / relative).read_bytes()
        schemas = [member for member in files if "/_data/contracts/" in member.name and member.name.endswith(".schema.json")]
        assert {Path(member.name).name for member in schemas} == {p.name for p in (root / "contracts").glob("*.schema.json")}
