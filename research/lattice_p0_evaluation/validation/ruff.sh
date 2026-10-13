#!/bin/bash
export PATH=/home/user-of-wsl/.cache/clawtune-parser-venv/bin:$PATH
cd /mnt/c/Users/29068/Desktop/ClawTune/.runtime/lattice-p0/before-source
python -m ruff check --output-format json services/sidecar/src/tool_time/_lattice_vendor/nodes.py services/sidecar/src/tool_time/_lattice_vendor/schemas.py services/sidecar/src/tool_time/lattice_kb.py services/sidecar/src/tool_time/resource_lattice.py services/sidecar/tests/test_lattice_time_kb.py > ../ruff-before.json
cd /mnt/c/Users/29068/Desktop/ClawTune
python -m ruff check --output-format json services/sidecar/src/tool_time/_lattice_vendor/nodes.py services/sidecar/src/tool_time/_lattice_vendor/schemas.py services/sidecar/src/tool_time/lattice_kb.py services/sidecar/src/tool_time/resource_lattice.py services/sidecar/tests/test_lattice_time_kb.py > .runtime/lattice-p0/ruff-after.json
python -m ruff check services/sidecar/src/tool_time/query.py services/sidecar/tests/test_lattice_query_policy.py scripts/command_rules/compare_lattice_p0.py > .runtime/lattice-p0/ruff-new.log