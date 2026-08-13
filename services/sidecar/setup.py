from setuptools import find_packages, setup


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
    install_requires=[
        "fastapi>=0.110",
        "httpx>=0.27",
        "pydantic>=2",
        "psutil>=5.9",
        "numpy>=1.26",
        "typing-extensions>=4.12",
        "uvicorn>=0.27",
        "prometheus-client>=0.20",
    ],
    extras_require={
        "dev": ["pytest>=8", "ruff>=0.6", "mypy>=1.10", "jsonschema>=4"],
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
