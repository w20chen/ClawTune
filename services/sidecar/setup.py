from setuptools import find_packages, setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist
from pathlib import Path
import shutil


def bundle_data(target):
    repo = Path(__file__).resolve().parents[2]
    source = repo if (repo / "contracts").is_dir() else Path(__file__).resolve().parent / "src/clawtune_kb/_data"
    for directory in ("contracts", "seeds/bootstrap-v1"):
        shutil.copytree(source / directory, target / directory, dirs_exist_ok=True)


class SourceWithContracts(sdist):
    """Source releases must rebuild without the surrounding Git checkout."""

    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        bundle_data(Path(base_dir) / "src/clawtune_kb/_data")


class BuildWithContracts(build_py):
    """Bundle canonical repo data when building a deployable sidecar wheel."""
    def run(self):
        super().run()
        # An incremental wheel build must not retain retired seeds from an
        # earlier build directory. Only remove this build's generated data.
        build_root = Path(self.build_lib).resolve()
        seed_target = (build_root / "clawtune_kb/_data/seeds").resolve()
        if not seed_target.is_relative_to(build_root):
            raise ValueError("seed build target is outside build_lib")
        if seed_target.exists():
            shutil.rmtree(seed_target)
        bundle_data(build_root / "clawtune_kb/_data")


# Compatibility metadata for installers that fall back from PEP 660 editable
# installs to setup.py develop. Keep this mirror aligned with pyproject.toml;
# otherwise they silently create UNKNOWN 0.0.0 and skip all dependencies.
setup(
    name="clawtune-sidecar",
    version="0.1.0",
    description="ClawTune hardware-aware sidecar for OpenClaw",
    python_requires=">=3.10",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    cmdclass={"build_py": BuildWithContracts, "sdist": SourceWithContracts},
    install_requires=[
        "fastapi>=0.110",
        "httpx>=0.27",
        "pydantic>=2",
        "psutil>=5.9",
        "numpy>=1.26",
        "jsonschema>=4",
        "pyyaml>=6",
        "typing-extensions>=4.12",
        "uvicorn>=0.27",
        "prometheus-client>=0.20",
    ],
    extras_require={
        "dev": ["pytest>=8", "ruff>=0.6", "mypy>=1.10", "jsonschema>=4", "setuptools>=68", "wheel"],
    },
    entry_points={
        "console_scripts": [
            "clawtune-launch=clawtune_sidecar.launcher:main",
            "clawtune-sidecar=clawtune_sidecar.main:main",
            "clawtune-setup=clawtune_sidecar.cli:setup_main",
            "clawtune-doctor=clawtune_sidecar.cli:doctor_main",
            "clawtune-check=clawtune_sidecar.cli:check_main",
        ],
    },
    package_data={
        "tool_resource": ["_mvdan_adapter/*"],
        "tool_time": ["_lattice_vendor/LICENSE", "_lattice_vendor/VENDORED.md"],
    },
)
