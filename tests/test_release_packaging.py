"""Source releases must carry runtime data without a parent checkout."""
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def test_source_release_includes_seed_and_public_contracts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / "repo"
    package = fixture / "services/sidecar"
    module = package / "src/clawtune_kb"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(root / "services/sidecar/setup.py", package / "setup.py")
    shutil.copytree(root / "contracts", fixture / "contracts")
    shutil.copytree(root / "seeds/bootstrap-v1", fixture / "seeds/bootstrap-v1")
    output = tmp_path / "dist"
    result = subprocess.run([sys.executable, "setup.py", "sdist", "--dist-dir", str(output)],
                            cwd=package, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        files = [member for member in archive.getmembers() if member.isfile()]
        seeds = [member for member in files if "/_data/seeds/" in member.name]
        assert len(seeds) == 4
        for member in seeds:
            assert "/seeds/bootstrap-v1/" in member.name
            assert archive.extractfile(member).read() == (root / "seeds/bootstrap-v1" / Path(member.name).name).read_bytes()
        schemas = [member for member in files if "/_data/contracts/" in member.name and member.name.endswith(".schema.json")]
        assert {Path(member.name).name for member in schemas} == {p.name for p in (root / "contracts").glob("*.schema.json")}
