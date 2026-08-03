from setuptools import find_packages, setup

setup(
    name="agent-scheduler",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "fastapi>=0.110",
        "httpx>=0.27",
        "pydantic>=2",
        "psutil>=5.9",
        "numpy>=1.26",
        "uvicorn>=0.27",
        "prometheus-client>=0.20",
    ],
    extras_require={
        "dev": [
            "pytest>=8",
            "ruff>=0.6",
            "mypy>=1.10",
            "jsonschema>=4",
        ],
    },
    entry_points={
        "console_scripts": [
            "claw-launch=agent_scheduler.launcher:main",
        ],
    },
    package_data={
        "tool_resource": ["_mvdan_adapter/*"],
    },
)
